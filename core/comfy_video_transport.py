"""Bounded native Comfy transport for explicitly submitted local video work."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from .generation_transport import local_endpoint, strict_json

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 256 * 1024 * 1024
_TOKEN = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_NODE = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("Invalid owned video job identity.")
    return value


def output_descriptor(value, *, output_token):
    """Admit only this job's single-file, ordinary Comfy output descriptor."""
    token = _token(output_token)
    if not isinstance(value, dict) or value.get("type") != "output":
        raise ValueError("The video result is not an ordinary output file.")
    filename = value.get("filename")
    subfolder = value.get("subfolder", "")
    if (not isinstance(filename, str)
            or not re.fullmatch(re.escape("collectigen_" + token) + r"_[0-9]+_?\.mp4", filename)
            or subfolder not in ("", "collectigen", "video")):
        raise ValueError("The video output does not belong to this exact job.")
    return {"filename": filename, "subfolder": subfolder, "type": "output"}


class ComfyVideoTransport:
    def __init__(self, endpoint, *, targeted_interrupt=False):
        self.endpoint = local_endpoint(endpoint)
        self.address = urlsplit(self.endpoint)
        self.targeted_interrupt = targeted_interrupt is True

    def _request(self, method, path, *, body=None, content_type=None, limit=MAX_JSON_BYTES):
        connection = http.client.HTTPConnection(self.address.hostname, self.address.port, timeout=30)
        try:
            headers = {"Content-Type": content_type} if content_type else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                # Retain bounded structured diagnostics for this exact request;
                # never retry a rejected/lost submission based on an error string.
                raw = response.read(65_537)
                details = {}
                if len(raw) <= 65_536:
                    try:
                        candidate = strict_json(raw)
                        if isinstance(candidate, dict):
                            details = candidate
                    except (ValueError, TypeError, UnicodeError):
                        pass
                error = RuntimeError(f"Local Comfy returned HTTP {response.status}. " +
                    json.dumps(details, ensure_ascii=True)[:4096])
                error.status_code = response.status
                error.response_details = details
                raise error
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("The local Comfy response exceeds the supported size.")
            return raw
        finally:
            connection.close()

    def _json(self, method, path, payload=None, *, allow_empty=False):
        body = json.dumps(payload, allow_nan=False, ensure_ascii=False).encode() if payload is not None else None
        if body is not None and len(body) > MAX_JSON_BYTES:
            raise ValueError("The video API request exceeds the supported size.")
        raw = self._request(method, path, body=body,
            content_type="application/json" if body is not None else None)
        if allow_empty and not raw:
            return {}
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("The native Comfy response must be an object.")
        if value.get("error") or value.get("node_errors"):
            raise ValueError("Native Comfy rejected the reviewed video graph.")
        return value

    def system_stats(self):
        return self._json("GET", "/system_stats")

    def object_info(self, node_class):
        if not isinstance(node_class, str) or not _NODE.fullmatch(node_class):
            raise ValueError("Invalid native node class.")
        return self._json("GET", "/object_info/" + node_class)

    def submit(self, graph, *, prompt_id, client_id, manifest):
        # The caller must durably retain this ID and graph BEFORE this write.
        identifier = _token(prompt_id)
        result = self._json("POST", "/prompt", {"prompt": graph, "prompt_id": identifier,
            "client_id": _token(client_id), "extra_data": {"collectigen": manifest}})
        if result.get("prompt_id") != identifier:
            raise ValueError("Comfy did not acknowledge the exact retained prompt identity; reconcile before retrying.")
        return result

    def history(self, prompt_id):
        return self._json("GET", "/history/" + _token(prompt_id))

    def queue(self):
        return self._json("GET", "/queue")

    def cancel(self, prompt_id, *, running=False):
        identifier = _token(prompt_id)
        if running:
            if not self.targeted_interrupt:
                raise ValueError("This runtime has no qualified job-specific running cancellation; no global interrupt was sent.")
            return self._json("POST", "/interrupt", {"prompt_id": identifier}, allow_empty=True)
        return self._json("POST", "/queue", {"delete": [identifier]}, allow_empty=True)

    def upload_source(self, raw, *, source_sha256, output_token):
        if (not isinstance(raw, bytes) or not raw or len(raw) > MAX_SOURCE_BYTES
                or hashlib.sha256(raw).hexdigest() != source_sha256):
            raise ValueError("The video source does not match its reviewed bytes or size limit.")
        # Only canonical PNG bytes are accepted, never a caller-provided path/name.
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1 or image.width * image.height > 16_777_216:
                raise ValueError("The video source must be one bounded PNG image.")
            image.verify()
        name = "collectigen_" + _token(output_token) + "_source.png"
        boundary = "CollectiGen" + uuid4().hex
        parts = []
        for field, value in (("type", "input"), ("overwrite", "false")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode() + raw + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        response = strict_json(self._request("POST", "/upload/image", body=b"".join(parts),
            content_type="multipart/form-data; boundary=" + boundary))
        if (not isinstance(response, dict) or response.get("name") != name
                or response.get("subfolder", "") != "" or response.get("type") != "input"):
            raise ValueError("Comfy did not retain the exact owned source name; reconcile the upload before retrying.")
        return response

    def download_output(self, descriptor, *, output_token):
        admitted = output_descriptor(descriptor, output_token=output_token)
        return self._request("GET", "/view?" + urlencode(admitted), limit=MAX_RESULT_BYTES)
