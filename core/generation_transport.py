"""Bounded, loopback-only Swarm transport. No proxy, redirect or credentials."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import select
import socket
import struct
import time
from urllib.parse import urlsplit

MAX_MESSAGE = 96 * 1024 * 1024


def local_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Choose an HTTP loopback endpoint without credentials or a path.")
    port = parsed.port or 80
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field in generator response.")
        result[key] = value
    return result


def strict_json(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))


class LocalTransport:
    def __init__(self, endpoint):
        self.endpoint = local_endpoint(endpoint)
        self.address = urlsplit(self.endpoint)

    def post(self, route, payload, *, timeout=15, max_bytes=8 * 1024 * 1024):
        if not route.isalnum():
            raise ValueError("Invalid API route")
        if (type(timeout) not in (int, float) or not 0 < timeout <= 15
                or type(max_bytes) is not int or not 1 <= max_bytes <= 8 * 1024 * 1024):
            raise ValueError("Invalid bounded API observation limits")
        connection = http.client.HTTPConnection(self.address.hostname, self.address.port, timeout=timeout)
        try:
            connection.request("POST", "/API/" + route, json.dumps(payload).encode(),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Local generator returned HTTP {response.status}.")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError("Generator API response exceeds the bounded limit.")
            result = strict_json(raw)
            if not isinstance(result, dict):
                raise ValueError("Generator API response is not an object.")
            if result.get("error") or result.get("error_id"):
                raise RuntimeError("Local generator rejected the request; inspect its local status.")
            return result
        finally:
            connection.close()

    def events(self, payload, *, cancelled, interrupt, progress, timeout=900):
        """RFC 6455 text client: masked sends, bounded fragments, ping/close handling.

        No compression/extensions negotiated; only the private generation route.
        Interrupted or dropped streams never trigger a second submission.
        """
        key = base64.b64encode(os.urandom(16)).decode()
        sock = socket.create_connection((self.address.hostname, self.address.port), timeout=15)
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        interrupted = False

        def send(opcode, data):
            mask = os.urandom(4)
            length = len(data)
            header = bytes([0x80 | opcode, 0x80 | min(length, 126)])
            if length >= 65536:
                header = bytes([0x80 | opcode, 0xff]) + struct.pack("!Q", length)
            elif length >= 126:
                header += struct.pack("!H", length)
            sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

        def take(count):
            nonlocal interrupted
            while len(buffer) < count:
                if time.monotonic() > deadline:
                    raise TimeoutError("Generation timed out; reconcile the retained submission before retrying.")
                if cancelled() and not interrupted:
                    interrupt()
                    interrupted = True
                if not select.select([sock], [], [], 0.25)[0]:
                    progress("Waiting for the local generator" if not interrupted else "Waiting for cancellation acknowledgement")
                    continue
                chunk = sock.recv(min(65536, max(count - len(buffer), 1)))
                if not chunk:
                    raise ConnectionError("Generation stream ended without a close acknowledgement.")
                buffer.extend(chunk)
            result = bytes(buffer[:count]); del buffer[:count]
            return result

        try:
            sock.sendall((f"GET /API/GenerateText2ImageWS HTTP/1.1\r\nHost: {self.address.netloc}\r\n"
                          "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                header.extend(take(1))
                if len(header) > 16384:
                    raise ValueError("WebSocket handshake is too large.")
            lines = header.decode("ascii").split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, v in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (lines[0].split()[1] != "101" or headers.get("sec-websocket-accept") != expected
                    or headers.get("sec-websocket-extensions")
                    or headers.get("upgrade", "").lower() != "websocket"):
                raise ConnectionError("Local generator did not accept the WebSocket contract.")
            send(1, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
            message = bytearray()
            fragmented = False
            while True:
                first, second = take(2)
                final, opcode = bool(first & 0x80), first & 0x0f
                size = second & 0x7f
                if first & 0x70 or second & 0x80:
                    raise ValueError("Unsupported WebSocket frame flags.")
                if size == 126: size = struct.unpack("!H", take(2))[0]
                elif size == 127: size = struct.unpack("!Q", take(8))[0]
                if size > MAX_MESSAGE or len(message) + size > MAX_MESSAGE:
                    raise ValueError("Generator event exceeds the bounded limit.")
                if opcode >= 8 and (not final or size > 125):
                    raise ValueError("Malformed WebSocket control frame.")
                data = take(size)
                if opcode == 8:
                    if fragmented or (data and (len(data) < 2 or struct.unpack("!H", data[:2])[0] not in {1000, 1001})):
                        raise ConnectionError("Generator closed before completing the response.")
                    send(8, data)
                    return
                if opcode == 9: send(10, data); continue
                if opcode == 10: continue
                if opcode not in {0, 1} or (opcode == 0) != fragmented:
                    raise ValueError("Unsupported WebSocket message sequence.")
                message.extend(data)
                fragmented = not final
                if final:
                    event = strict_json(message.decode("utf-8")); message.clear()
                    if not isinstance(event, dict):
                        raise ValueError("Generation event is not an object.")
                    yield event
        finally:
            sock.close()
