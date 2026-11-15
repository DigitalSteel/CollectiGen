"""Synthetic pure-codec cases; no real server, image file, model or provider."""
import base64
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from core import lmstudio_description as codec

IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def request(**changes):
    values = dict(model_key="fixture/model", expected_instance_id="instance-A",
                  prompt="Describe this image. café", image_bytes=IMAGE,
                  image_mime="image/png", temperature=0, max_output_tokens=256)
    values.update(changes)
    return codec.encode_description(**values)


def response(**changes):
    values = dict(model_instance_id="instance-A",
                  output=[dict(type="message", content="One generated pixel.")])
    values.update(changes)
    return json.dumps(values, ensure_ascii=False).encode()


def test_exact_stateless_body_and_detached_correlation():
    result = request()
    body = json.loads(result.body)
    assert codec.ROUTE == "/api/v1/chat"
    assert body == dict(model="fixture/model", input=[
        dict(type="text", content="Describe this image. café"),
        dict(type="image", data_url="data:image/png;base64,"+base64.b64encode(IMAGE).decode()),
    ], store=False, stream=False, temperature=0.0, max_output_tokens=256)
    assert result.body == request().body
    assert result.expected_instance_id == "instance-A"
    assert result.image_sha256 == hashlib.sha256(IMAGE).hexdigest()
    assert result.prompt_sha256 == hashlib.sha256("Describe this image. café".encode()).hexdigest()
    assert "Describe" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.body = b"changed"


@pytest.mark.parametrize("changes,code", [
    ({"model_key": ""}, "invalid_model_key"),
    ({"model_key": " x"}, "invalid_model_key"),
    ({"model_key": "a\u200bb"}, "invalid_model_key"),
    ({"model_key": "a"*513}, "invalid_model_key"),
    ({"expected_instance_id": "x\n"}, "invalid_instance_id"),
    ({"expected_instance_id": True}, "invalid_instance_id"),
    ({"prompt": " \n\t"}, "invalid_prompt"),
    ({"prompt": "x"*(codec.MAX_PROMPT_CHARS+1)}, "invalid_prompt"),
    ({"prompt": "\ud800"}, "invalid_prompt"),
    ({"image_bytes": b""}, "invalid_image_bytes"),
    ({"image_bytes": bytearray(IMAGE)}, "invalid_image_bytes"),
    ({"image_bytes": b"x"*(codec.MAX_IMAGE_BYTES+1)}, "invalid_image_bytes"),
    ({"image_mime": "image/webp"}, "invalid_image_mime"),
    ({"image_mime": "image/png; charset=utf8"}, "invalid_image_mime"),
    ({"temperature": True}, "invalid_temperature"),
    ({"temperature": float("nan")}, "invalid_temperature"),
    ({"temperature": float("inf")}, "invalid_temperature"),
    ({"temperature": -0.1}, "invalid_temperature"),
    ({"temperature": 1.1}, "invalid_temperature"),
    ({"max_output_tokens": True}, "invalid_output_limit"),
    ({"max_output_tokens": 0}, "invalid_output_limit"),
    ({"max_output_tokens": 4097}, "invalid_output_limit"),
])
def test_encoder_rejects_invalid_inputs_without_echo(changes, code):
    with pytest.raises(codec.DescriptionError) as error:
        request(**changes)
    assert str(error.value) == code


def test_exact_input_limits_and_jpeg_are_supported_without_decoding():
    result = request(model_key="k"*512, expected_instance_id="i"*512,
                     prompt="é"*codec.MAX_PROMPT_CHARS,
                     image_bytes=b"x"*codec.MAX_IMAGE_BYTES,
                     image_mime="image/jpeg", temperature=1, max_output_tokens=4096)
    assert len(result.body) <= codec.MAX_BODY_BYTES
    assert json.loads(result.body)["input"][1]["data_url"].startswith("data:image/jpeg;base64,")


def test_body_byte_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(codec, "MAX_BODY_BYTES", 1)
    with pytest.raises(codec.DescriptionError, match="^request_too_large$"):
        request()


def test_decoder_preserves_description_but_discards_reasoning_and_unknown_payload():
    raw = response(output=[dict(type="reasoning", content="PRIVATE-THINKING"),
                           dict(type="message", content="  First."),
                           dict(type="message", content="Second.  ")],
                   stats=dict(input_tokens=12, total_output_tokens=4,
                              tokens_per_second=2.5, vendor_private="not retained"),
                   future_extension=dict(secret="not retained"))
    req = request()
    result = codec.decode_description(raw, req)
    assert result.description == "  First.\nSecond.  "
    assert result.model_key == req.model_key and result.instance_id == "instance-A"
    assert result.request_sha256 == hashlib.sha256(req.body).hexdigest()
    assert result.statistics.input_tokens == 12
    assert result.statistics.tokens_per_second == 2.5
    assert result.statistics.model_load_time_seconds is None
    assert "PRIVATE" not in repr(result) and "First" not in repr(result)
    assert not hasattr(result, "raw_output") and not hasattr(result, "reasoning")
    with pytest.raises(FrozenInstanceError):
        result.description = "changed"
    with pytest.raises(FrozenInstanceError):
        result.statistics.input_tokens = 99


@pytest.mark.parametrize("raw,code", [
    (b"", "invalid_response_bytes"),
    (b"x"*(codec.MAX_RESPONSE_BYTES+1), "invalid_response_bytes"),
    (b"\xff", "invalid_response"),
    (b"{PRIVATE-RESPONSE", "invalid_response"),
    (b'{"x":1,"x":2}', "invalid_response"),
    (b'{"x":NaN}', "invalid_response"),
    (b"[]", "invalid_response"),
    (response(model_instance_id="instance-B"), "instance_mismatch"),
    (response(model_instance_id=""), "invalid_response_instance"),
    (response(response_id=None), "unexpected_stateful_response"),
    (response(response_id="resp_123"), "unexpected_stateful_response"),
    (response(output=[]), "invalid_output"),
    (response(output=[{}]*257), "invalid_output"),
    (response(output=["PRIVATE"]), "invalid_output_item"),
    (response(output=[dict(type="tool_call", tool="PRIVATE")]), "unsupported_output_item"),
    (response(output=[dict(type="invalid_tool_call")]), "unsupported_output_item"),
    (response(output=[dict(type="future_item", content="PRIVATE")]), "unsupported_output_item"),
    (response(output=[dict(type="message", content=123)]), "invalid_output_text"),
    (response(output=[dict(type="message", content="x"*(codec.MAX_DESCRIPTION_CHARS+1))]), "invalid_output_text"),
    (response(output=[dict(type="reasoning", content="PRIVATE")]), "empty_description"),
    (response(output=[dict(type="message", content=" \n")]), "empty_description"),
    (response(stats=[]), "invalid_statistics"),
    (response(stats=dict(input_tokens=True)), "invalid_statistics"),
    (response(stats=dict(tokens_per_second=-1)), "invalid_statistics"),
    (b'{"model_instance_id":"instance-A","output":[{"type":"message","content":"PRIVATE"}],"stats":{"tokens_per_second":1e999}}', "invalid_statistics"),
], ids=["empty-bytes", "response-too-large", "bad-utf8", "bad-json", "duplicate-json",
        "nonfinite-json", "wrong-root", "wrong-instance", "empty-instance",
        "null-stateful-id", "stateful-id", "empty-output", "too-many-items",
        "nonobject-item", "tool-call", "invalid-tool-call", "future-item", "nontext",
        "text-too-large", "reasoning-only", "blank-text", "nonobject-stats",
        "boolean-stat", "negative-stat", "overflow-stat"])
def test_decoder_refusals_are_bounded_content_free(raw, code):
    with pytest.raises(codec.DescriptionError) as error:
        codec.decode_description(raw, request())
    assert str(error.value) == code
    assert error.value.__context__ is None


def test_description_join_limit_counts_separator_and_exact_limit():
    exact = "x"*codec.MAX_DESCRIPTION_CHARS
    assert len(codec.decode_description(response(output=[dict(type="message", content=exact)]), request()).description) == len(exact)
    with pytest.raises(codec.DescriptionError, match="^description_too_large$"):
        codec.decode_description(response(output=[dict(type="message", content=exact),
                                                   dict(type="message", content="x")]), request())


def test_exact_response_and_item_limits_and_unicode():
    raw = response(extension="")
    padded = response(extension=" "*(codec.MAX_RESPONSE_BYTES-len(raw)))
    assert len(padded) == codec.MAX_RESPONSE_BYTES
    assert codec.decode_description(padded, request()).description == "One generated pixel."
    many = [dict(type="message", content="é") for _ in range(codec.MAX_OUTPUT_ITEMS)]
    assert codec.decode_description(response(output=many), request()).description.count("é") == 256


def test_deep_and_wrong_raw_types_are_sanitized():
    with pytest.raises(codec.DescriptionError, match="^invalid_response$"):
        codec.decode_description(b"["*1500+b"0"+b"]"*1500, request())
    for raw in [response().decode(), bytearray(response()), None]:
        with pytest.raises(codec.DescriptionError, match="^invalid_response_bytes$"):
            codec.decode_description(raw, request())


def test_all_documented_stats_detached_and_load_time_is_only_observation():
    values = {name: 0 for name in codec.STAT_FIELDS}
    values["model_load_time_seconds"] = 2.3
    result = codec.decode_description(response(stats=values), request())
    values["model_load_time_seconds"] = 0
    assert result.statistics.model_load_time_seconds == 2.3
    assert not hasattr(result, "inference_ready") and not hasattr(result, "no_jit")


def test_bad_correlation_is_refused():
    with pytest.raises(codec.DescriptionError, match="^invalid_request_correlation$"):
        codec.decode_description(response(), object())
    with pytest.raises(codec.DescriptionError, match="^invalid_request_correlation$"):
        codec.decode_description(response(), replace(request(), expected_instance_id="bad\n"))


def test_codec_operations_have_no_io_or_process_side_effects(monkeypatch):
    import builtins
    import io
    import os
    from pathlib import Path
    import socket
    import subprocess

    raw = response()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unexpected side effect")

    with monkeypatch.context() as guard:
        for owner, name in [(builtins,"open"),(io,"open"),(Path,"open"),
                            (socket,"socket"),(socket,"getaddrinfo"),
                            (subprocess,"Popen"),(os,"system")]:
            guard.setattr(owner, name, forbidden)
        result = codec.decode_description(raw, request())
        assert result.description == "One generated pixel."
    assert calls == []
