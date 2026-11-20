"""Text-only vLLM transport for a reviewed Model Chat request."""

import http.client
import json
import socket
from threading import Event, Thread
from time import monotonic
from urllib.parse import urlsplit

from core.generation_transport import local_endpoint, strict_json
from .model_chat_controller import ChatEvent, ChatProviderError, ChatTarget


MAX_RESPONSE_BYTES = 1024 * 1024


class VllmChatError(ChatProviderError):
    """A stable diagnostic that omits response bodies and credentials."""

    @property
    def public_message(self):
        return {
            "vllm_authentication_required": "The server requires authentication. Reopen the connection and check the API token.",
            "vllm_authentication_refused": "The server refused authentication. Reopen the connection and check the API token.",
            "vllm_model_or_route_missing": "The model or chat route was unavailable. Check the model and server endpoint.",
            "vllm_model_not_listed": "The server did not list that model. Check its exact served name and reconnect.",
            "vllm_incompatible_completion": "The server returned an incompatible reply. Check the selected model and chat configuration.",
            "vllm_request_timeout": "The request timed out. Local help is still available.",
            "vllm_context_changed": "Settings changed. Disconnect and reopen help to review current information.",
            "vllm_manager_admission_changed": "Reopen Manager from the suite installation before sending the question.",
        }.get(str(self), "The vLLM request did not complete. Check the connection or view local help.")


class VllmChatClient:
    def __init__(self, endpoint, target, *, credential=lambda: None, context_is_current=lambda: True):
        if not isinstance(endpoint, str) or any(ch.isspace() or ord(ch) < 32 for ch in endpoint):
            raise VllmChatError("vllm_endpoint_invalid")
        address = urlsplit(endpoint)
        if (address.scheme not in {"http", "https"} or not address.hostname
                or address.username is not None or address.password is not None
                or address.query or address.fragment or address.path.rstrip("/") not in {"", "/v1"}):
            raise VllmChatError("vllm_endpoint_invalid")
        if address.scheme == "http":
            address = urlsplit(local_endpoint(f"http://{address.netloc}"))
        if (type(target) is not ChatTarget or target.backend_id != "vllm" or not target.ready
                or not callable(credential)):
            raise VllmChatError("vllm_target_not_admitted")
        self.address = address
        self.target = target
        self.credential = credential
        self.context_is_current = context_is_current

    def stream_chat(self, request, cancelled):
        """Yield one terminal text event and use the existing chat executor."""
        if request.target != self.target or not 1 <= request.output_tokens <= 4096:
            raise VllmChatError("vllm_target_or_output_changed")
        messages = request.messages
        if (len(messages) != 2 or tuple(message.role for message in messages) != ("system", "user")
                or any(not isinstance(message.text, str) or not message.text for message in messages)):
            raise VllmChatError("vllm_reviewed_context_required")
        raw = json.dumps({
            "model": self.target.model,
            "messages": [{"role": message.role, "content": message.text} for message in messages],
            "max_tokens": request.output_tokens,
            "stream": False,
        }, ensure_ascii=False).encode("utf-8")
        if len(raw) > 131072 or not 1 <= request.timeout_seconds <= 120:
            raise VllmChatError("vllm_request_bound")
        if not self.context_is_current():
            raise VllmChatError("vllm_context_changed")
        data = self._request("POST", "/v1/chat/completions", raw, cancelled, request.timeout_seconds)
        yield ChatEvent(_assistant_text(data, self.target.model), True)

    def check_model(self, cancelled):
        """Check only the server model list, before any contextual messages are sent."""
        data = self._request("GET", "/v1/models", None, cancelled, 8)
        try:
            document = strict_json(data)
            rows = document["data"]
            if (type(rows) is not list
                    or any(type(row) is not dict or type(row.get("id")) is not str for row in rows)):
                raise ValueError
            count = sum(row["id"] == self.target.model for row in rows)
        except (ValueError, KeyError, TypeError, RecursionError):
            raise VllmChatError("vllm_response_format") from None
        if count != 1:
            raise VllmChatError("vllm_model_not_listed")

    def _request(self, method, route, raw, cancelled, seconds):
        deadline = monotonic() + seconds

        def check():
            if cancelled():
                raise InterruptedError("vllm_request_cancelled")
            if monotonic() >= deadline:
                raise VllmChatError("vllm_request_timeout")

        check()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Connection": "close"}
        token = self.credential()
        if token is not None:
            if (type(token) is not str or not 1 <= len(token) <= 4096
                    or any(not 33 <= ord(ch) <= 126 for ch in token)):
                raise VllmChatError("vllm_credential_invalid")
            headers["Authorization"] = "Bearer " + token
        cls = http.client.HTTPSConnection if self.address.scheme == "https" else http.client.HTTPConnection
        connection = cls(self.address.hostname, self.address.port, timeout=min(seconds, 5))
        stop = Event()
        watcher = abort_socket = response = None
        try:
            connection.connect()
            check()
            abort_socket = socket.socket.dup(connection.sock)

            def interrupt_wait():
                while not stop.wait(0.02):
                    if cancelled() or monotonic() >= deadline:
                        try:
                            abort_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return

            watcher = Thread(target=interrupt_wait, name="vllm-chat-cancel", daemon=True)
            watcher.start()
            if method == "POST" and not self.context_is_current():
                raise VllmChatError("vllm_context_changed")
            check()
            connection.request(method, route, raw, headers)
            response = connection.getresponse()
            check()
            if response.status != 200:
                code = {401: "authentication_required", 403: "authentication_refused",
                        404: "model_or_route_missing"}.get(response.status, "request_refused")
                raise VllmChatError(f"vllm_{code}")
            if (response.getheader("Content-Type", "").split(";")[0].strip().lower() != "application/json"
                    or response.getheader("Content-Encoding", "identity").strip().lower() != "identity"):
                raise VllmChatError("vllm_response_format")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            check()
            if len(data) > MAX_RESPONSE_BYTES:
                raise VllmChatError("vllm_response_bound")
            return data
        except InterruptedError:
            raise
        except (OSError, http.client.HTTPException):
            check()
            raise VllmChatError("vllm_endpoint_unavailable_or_request_interrupted") from None
        finally:
            stop.set()
            if watcher is not None:
                watcher.join()
            if response is not None:
                response.close()
            connection.close()
            if abort_socket is not None:
                abort_socket.close()


def _assistant_text(data, model):
    try:
        record = strict_json(data)
        if type(record) is not dict or record.get("error") or record.get("model") != model:
            raise ValueError
        choices = record["choices"]
        if type(choices) is not list or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        message = choice["message"]
        if (choice.get("index") != 0 or choice.get("finish_reason") not in {"stop", "length"}
                or message.get("role") != "assistant" or message.get("tool_calls")
                or message.get("function_call")):
            raise ValueError
        content = message["content"]
        if type(content) is not str or not content.strip():
            raise ValueError
        return content
    except (AttributeError, KeyError, ValueError, TypeError, RecursionError):
        raise VllmChatError("vllm_incompatible_completion") from None
