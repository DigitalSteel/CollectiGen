"""Pure native-v1 description bytes, not an inference or resource capability.

No HTTP, file/image decoding, credential, provider, model or evidence operation.
Caller-supplied MIME is not pixel validation. Instance correlation after a reply
cannot prevent JIT loading/eviction; a later execution owner must admit policy.
"""
import base64
from dataclasses import dataclass
import hashlib
import json
import math
import unicodedata

from core.generation_transport import strict_json

ROUTE = "/api/v1/chat"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_BODY_BYTES = 12 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PROMPT_CHARS = 32768
MAX_DESCRIPTION_CHARS = 131072
MAX_OUTPUT_ITEMS = 256
STAT_FIELDS = (
    "input_tokens", "total_output_tokens", "reasoning_output_tokens",
    "tokens_per_second", "time_to_first_token_seconds", "model_load_time_seconds",
)


class DescriptionError(ValueError):
    """Only stable content-free codes escape the codec."""


def _require(condition, code):
    if not condition:
        raise DescriptionError(code)


def _identifier(value, code):
    _require(type(value) is str and 0 < len(value) <= 512, code)
    _require(value == value.strip() and not any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value
    ), code)
    return value


def _text(value, limit, code):
    _require(type(value) is str and len(value) <= limit, code)
    _require(not any(unicodedata.category(c) == "Cs" for c in value), code)
    return value


@dataclass(frozen=True, slots=True, repr=False)
class DescriptionRequest:
    body: bytes
    model_key: str
    expected_instance_id: str
    image_sha256: str
    prompt_sha256: str
    temperature: float
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class DescriptionStatistics:
    input_tokens: int | float | None = None
    total_output_tokens: int | float | None = None
    reasoning_output_tokens: int | float | None = None
    tokens_per_second: int | float | None = None
    time_to_first_token_seconds: int | float | None = None
    model_load_time_seconds: int | float | None = None


@dataclass(frozen=True, slots=True, repr=False)
class DescriptionResult:
    description: str
    model_key: str
    instance_id: str
    request_sha256: str
    statistics: DescriptionStatistics


def encode_description(*, model_key, expected_instance_id, prompt, image_bytes,
                       image_mime, temperature, max_output_tokens):
    """Construct one non-storing request; do not infer readiness from these data."""
    _identifier(model_key, "invalid_model_key")
    _identifier(expected_instance_id, "invalid_instance_id")
    _text(prompt, MAX_PROMPT_CHARS, "invalid_prompt")
    _require(bool(prompt.strip()), "invalid_prompt")
    _require(type(image_bytes) is bytes and 0 < len(image_bytes) <= MAX_IMAGE_BYTES,
             "invalid_image_bytes")
    _require(type(image_mime) is str and image_mime in {"image/png", "image/jpeg"},
             "invalid_image_mime")
    _require(type(temperature) in (int, float) and 0 <= temperature <= 1,
             "invalid_temperature")
    _require(type(max_output_tokens) is int and 1 <= max_output_tokens <= 4096,
             "invalid_output_limit")
    body = json.dumps({
        "model": model_key,
        "input": [
            {"type": "text", "content": prompt},
            {"type": "image", "data_url": "data:" + image_mime + ";base64," +
             base64.b64encode(image_bytes).decode("ascii")},
        ],
        "store": False, "stream": False,
        "temperature": float(temperature), "max_output_tokens": max_output_tokens,
    }, ensure_ascii=False, allow_nan=False, sort_keys=True,
       separators=(",", ":")).encode("utf-8")
    _require(len(body) <= MAX_BODY_BYTES, "request_too_large")
    return DescriptionRequest(
        body, model_key, expected_instance_id,
        hashlib.sha256(image_bytes).hexdigest(),
        hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        float(temperature), max_output_tokens,
    )


def decode_description(raw, request):
    """Return unreviewed text, never a write, server stop or no-JIT assertion."""
    _require(type(request) is DescriptionRequest, "invalid_request_correlation")
    _identifier(request.expected_instance_id, "invalid_request_correlation")
    _identifier(request.model_key, "invalid_request_correlation")
    _require(type(request.body) is bytes, "invalid_request_correlation")
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_RESPONSE_BYTES,
             "invalid_response_bytes")
    invalid = False
    try:
        value = strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        invalid = True
        value = None
    # Raise outside the parser's except block: its exception can retain raw input.
    _require(not invalid and type(value) is dict, "invalid_response")
    _require("response_id" not in value, "unexpected_stateful_response")
    instance = _identifier(value.get("model_instance_id"), "invalid_response_instance")
    _require(instance == request.expected_instance_id, "instance_mismatch")
    output = value.get("output")
    _require(type(output) is list and 0 < len(output) <= MAX_OUTPUT_ITEMS,
             "invalid_output")
    messages = []
    text_size = 0
    for item in output:
        _require(type(item) is dict, "invalid_output_item")
        kind = item.get("type")
        _require(type(kind) is str and kind in {"message", "reasoning"},
                 "unsupported_output_item")
        text = _text(item.get("content"), MAX_DESCRIPTION_CHARS, "invalid_output_text")
        if kind == "message":
            text_size += len(text) + (1 if messages else 0)
            _require(text_size <= MAX_DESCRIPTION_CHARS, "description_too_large")
            messages.append(text)
    description = "\n".join(messages)
    _require(bool(description.strip()), "empty_description")
    stats = value.get("stats", {})
    _require(type(stats) is dict, "invalid_statistics")
    observed = {}
    for name in STAT_FIELDS:
        if name not in stats:
            continue
        number = stats[name]
        _require(type(number) in (int, float) and number >= 0 and
                 (type(number) is int or math.isfinite(number)), "invalid_statistics")
        observed[name] = number
    return DescriptionResult(
        description, request.model_key, instance, hashlib.sha256(request.body).hexdigest(),
        DescriptionStatistics(**observed),
    )
