"""In-memory chat over an injected, already admitted streaming backend.

No network, model load, credentials, persistence or process ownership here.
The host retains the executor/controller until close_held is false. Cancellation
asks the client to abort this request; it never stops or unloads a shared server.
"""
from collections import deque
from dataclasses import dataclass
from threading import Event, Lock, get_ident
from typing import Callable, Iterable, Protocol


@dataclass(frozen=True)
class ChatTarget:
    backend_id: str
    model: str
    digest: str
    ready: bool
    unavailable_reason: str = "Refresh and qualify this model before chatting."
    context_length: int = 4096

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in (self.backend_id, self.model, self.digest)):
            raise ValueError("An exact configured backend and model identity are required.")
        if type(self.ready) is not bool or type(self.context_length) is not int or not 512 <= self.context_length <= 262144:
            raise ValueError("Invalid model readiness or context limit.")


@dataclass(frozen=True)
class ChatMessage:
    role: str
    text: str
    state: str = "complete"


@dataclass(frozen=True)
class ChatRequest:
    request_id: int
    target: ChatTarget
    messages: tuple[ChatMessage, ...]
    output_tokens: int
    timeout_seconds: int = 120

    def ollama_payload(self):
        """Payload only; host supplies its guarded transport and thinking policy."""
        return {"model": self.target.model, "messages": [
            {"role": m.role, "content": m.text} for m in self.messages],
            "stream": True, "truncate": False,
            "options": {"num_ctx": self.target.context_length, "num_predict": self.output_tokens}}


@dataclass(frozen=True)
class ChatEvent:
    text: str = ""
    done: bool = False


def ollama_event(value):
    """Map native chat NDJSON; never expose thinking or execute tool calls."""
    if type(value) is not dict or value.get("error"):
        raise ValueError("The backend rejected the chat request.")
    message = value.get("message", {})
    if type(message) is not dict or message.get("tool_calls"):
        raise ValueError("Tool responses are unsupported in this chat.")
    text, done = message.get("content", ""), value.get("done")
    if type(text) is not str or type(done) is not bool:
        raise ValueError("The backend returned an invalid chat stream.")
    return ChatEvent(text, done)


class StreamingChatClient(Protocol):
    def stream_chat(self, request: ChatRequest, cancelled: Callable[[], bool]) -> Iterable[ChatEvent]:
        """Bound I/O by request.timeout_seconds and interrupt reads on cancellation.

        Validate the pinned backend/model before submission; return only assistant
        content and an explicit terminal event. Close only this request's socket
        or iterator. Never stop/load/unload the shared server or execute tools.
        """


class ModelChatController:
    def __init__(self, client: StreamingChatClient, executor, *,
                 max_context_bytes=8192, max_input_bytes=4096,
                 max_response_bytes=32768, max_turns=12, output_tokens=512):
        limits = (max_context_bytes, max_input_bytes, max_response_bytes, max_turns, output_tokens)
        if any(type(v) is not int or v <= 0 for v in limits) or max_input_bytes > max_context_bytes:
            raise ValueError("Positive bounded chat limits are required.")
        if max_context_bytes > 65536 or max_response_bytes > 262144 or max_turns > 32 or output_tokens > 4096:
            raise ValueError("Chat limits exceed the supported bounds.")
        self.client, self.executor = client, executor
        self.max_context_bytes, self.max_input_bytes = max_context_bytes, max_input_bytes
        self.max_response_bytes, self.max_turns, self.output_tokens = max_response_bytes, max_turns, output_tokens
        self.target = None
        self.messages = []
        self.status = "Choose a ready configured model."
        self.closed = False
        self._history = []
        self._future = None
        self._cancel = None
        self._serial = 0
        self._generation = 0
        self._active_generation = None
        self._events = deque()
        self._event_lock = Lock()
        self._owner = get_ident()
        self.context_omitted_turns = 0

    def _guard(self):
        if get_ident() != self._owner:
            raise RuntimeError("Chat presentation must stay on its owning thread.")

    @property
    def busy(self):
        return self._future is not None

    @property
    def close_held(self):
        return self.busy or bool(getattr(self.executor, "close_held", False))

    @property
    def can_send(self):
        return not self.closed and not self.close_held and self.target is not None and self.target.ready

    def select_target(self, target):
        self._guard()
        if target is not None and type(target) is not ChatTarget:
            raise TypeError("A host-qualified ChatTarget is required.")
        if target != self.target:
            self.new_chat()
        self.target = target
        if not self.busy:
            self.status = self._ready_status()

    def _ready_status(self):
        if self.target is None:
            return "Choose a ready configured model."
        return "Ready. Chat stays in this panel only." if self.target.ready else self.target.unavailable_reason

    def new_chat(self):
        self._guard()
        self.stop()
        self._generation += 1
        self.messages.clear()
        self._history.clear()
        self.context_omitted_turns = 0
        with self._event_lock:
            self._events.clear()
        self.status = "Stopping the previous request…" if self.busy else self._ready_status()

    def send(self, text):
        self._guard()
        if not self.can_send:
            raise ValueError("Choose a ready model and wait for the previous request to finish.")
        if type(text) is not str or not text.strip():
            raise ValueError("Enter a message.")
        if len(text.encode("utf-8")) > self.max_input_bytes:
            raise ValueError(f"Message exceeds the {self.max_input_bytes}-byte limit.")
        request_messages = [ChatMessage("user", text)]
        remaining = self.max_context_bytes - len(text.encode("utf-8"))
        included = 0
        for pair in reversed(self._history):
            size = sum(len(m.text.encode("utf-8")) for m in pair)
            if included >= self.max_turns - 1 or size > remaining:
                break
            request_messages[0:0] = pair
            remaining -= size
            included += 1
        self.context_omitted_turns = len(self._history) - included
        self._serial += 1
        request = ChatRequest(self._serial, self.target, tuple(request_messages), self.output_tokens)
        self._cancel = Event()
        generation = self._generation
        self._active_generation = generation
        self.messages.extend((ChatMessage("user", text), ChatMessage("assistant", "", "streaming")))
        self.messages[:] = self.messages[-2*self.max_turns:]
        self.status = "Generating…" + (f" {self.context_omitted_turns} older turn(s) omitted from context." if self.context_omitted_turns else "")
        try:
            self._future = self.executor.submit(self._work, request, self._cancel, generation)
        except Exception:
            self.messages[-1] = ChatMessage("assistant", "", "error")
            self.status = "Chat worker unavailable. Retry after the worker is ready."
            raise RuntimeError("Chat worker unavailable.") from None
        return request

    def _work(self, request, cancel, generation):
        chunks, size, done = [], 0, False
        stream = None
        try:
            if cancel.is_set():
                return ("cancelled", "")
            stream = iter(self.client.stream_chat(request, cancel.is_set))
            for event in stream:
                if cancel.is_set():
                    return ("cancelled", "")
                if type(event) is not ChatEvent or type(event.text) is not str or type(event.done) is not bool:
                    raise ValueError("Invalid stream event.")
                if done:
                    raise ValueError("Content arrived after completion.")
                size += len(event.text.encode("utf-8"))
                if size > self.max_response_bytes:
                    cancel.set()
                    return ("error", "Response limit reached. Start a shorter request.")
                chunks.append(event.text)
                with self._event_lock:
                    # Coalesce adjacent deltas; queue size stays bounded even if
                    # the host has not polled yet. Total text is bounded above.
                    if self._events and self._events[-1][0] == generation:
                        old_generation, old_text = self._events.pop()
                        self._events.append((old_generation, old_text + event.text))
                    else:
                        self._events.append((generation, event.text))
                done = event.done
            if cancel.is_set():
                return ("cancelled", "")
            if not done:
                return ("error", "Connection ended before completion. Retry when the backend is ready.")
            return ("complete", "".join(chunks))
        except Exception:
            return ("cancelled", "") if cancel.is_set() else ("error", "Chat failed. Check the selected backend, then retry.")
        finally:
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    return ("error", "Request cleanup failed. Check the backend before retrying.")

    def poll(self):
        self._guard()
        with self._event_lock:
            events = tuple(self._events)
            self._events.clear()
        for generation, text in events:
            if generation == self._generation and not self.closed and self.messages and not self._cancel.is_set():
                old = self.messages[-1]
                self.messages[-1] = ChatMessage("assistant", old.text + text, "streaming")
        if self._future is None or not self._future.done():
            return
        try:
            state, value = self._future.result()
        except Exception:
            state, value = "error", "Chat worker failed. Check the backend before retrying."
        self._future = None
        current = self._active_generation == self._generation and not self.closed
        if not current:
            self.status = "Closed." if self.closed else self._ready_status()
            return
        if self._cancel.is_set() and state == "complete":
            state = "cancelled"
        old = self.messages[-1]
        self.messages[-1] = ChatMessage("assistant", value if state == "complete" else old.text, state)
        if state == "complete":
            self._history.append(tuple(self.messages[-2:]))
            self._history[:] = self._history[-self.max_turns:]
            self.status = "Reply complete."
        elif state == "cancelled":
            self.status = "Request ended after Stop. Partial reply is excluded from context."
        else:
            self.status = value

    def stop(self):
        self._guard()
        if self._future is not None:
            self._cancel.set()
            self.status = "Stopping… waiting for this request to finish."

    def begin_close(self):
        self._guard()
        self.stop()
        self.closed = True
        self._generation += 1
        self._history.clear()
        self.messages.clear()
        self.status = "Closing… waiting for this request to finish." if self.close_held else "Closed."
