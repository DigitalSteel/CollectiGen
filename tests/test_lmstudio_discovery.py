"""Pure synthetic metadata checks; no endpoint, credential or model access."""
import ast
from dataclasses import FrozenInstanceError
import inspect
import json
import traceback

import pytest

from core import lmstudio_discovery as discovery
from core.lmstudio_discovery import DiscoveryError, parse_models


def model(**changes):
    value = dict(key="publisher/example", type="llm", display_name="Example",
                 size_bytes=1234, max_context_length=131072, loaded_instances=[])
    value.update(changes)
    return value


def encode(*records, **extra):
    return json.dumps(dict(models=list(records), **extra)).encode()


def instance(identity="instance-A", **config):
    return dict(id=identity, config=dict(context_length=65536, **config))


def test_keys_instances_capabilities_and_config_remain_distinct():
    result = parse_models(encode(model(loaded_instances=[
        instance(parallel=2, flash_attention=True), instance("instance-B")],
        capabilities=dict(vision=True, trained_for_tool_use=False))))
    selected = result.model("publisher/example")
    assert selected.key != selected.loaded_instances[0].instance_id
    assert selected.vision is True and selected.trained_for_tool_use is False
    assert selected.max_context_length == 131072
    assert selected.loaded_instances[0].config.context_length == 65536
    assert selected.loaded_instances[0].config.parallel == 2
    assert selected.loaded_instances[0].config.flash_attention is True
    assert result.loaded_instance("instance-B")[0] is selected
    assert result.loaded_instance("instance-B")[1].config.parallel is None
    assert selected.known_model_type


@pytest.mark.parametrize("capabilities,expected", [({}, None), ({"vision": False}, False)])
def test_missing_and_false_claims_are_not_ready(capabilities, expected):
    selected = parse_models(encode(model(capabilities=capabilities))).models[0]
    assert selected.vision is expected
    assert selected.trained_for_tool_use is None
    assert selected.loaded_instances == ()
    assert not hasattr(selected, "ready")


def test_forward_fields_empty_list_unknown_type_and_nullable_context():
    assert parse_models(encode()).models == ()
    selected = parse_models(encode(model(type="future-type", max_context_length=None,
        future={"unknown": [1, "opaque"]}), future_top=True)).models[0]
    assert not selected.known_model_type
    assert selected.model_type == "future-type" and selected.max_context_length is None
    assert not hasattr(selected, "future")
    for kind in ("embedding", "decision"):
        item = parse_models(encode(model(type=kind))).models[0]
        assert item.known_model_type and item.vision is None


def test_results_are_detached_and_deeply_immutable():
    data = model(loaded_instances=[instance()])
    selected = parse_models(encode(data)).models[0]
    data["loaded_instances"][0]["config"]["context_length"] = 1
    assert selected.loaded_instances[0].config.context_length == 65536
    for target, attr in [(selected, "key"), (selected.loaded_instances[0], "instance_id"),
                         (selected.loaded_instances[0].config, "context_length")]:
        with pytest.raises(FrozenInstanceError):
            setattr(target, attr, None)
    assert isinstance(selected.loaded_instances, tuple)


@pytest.mark.parametrize("raw", [b"", b"not-json", b"[]", b"{}", b'{"models":null}',
    b'{"models":{}}', b'{"models":[null]}', b'{"models":[],"models":[]}',
    b'{"models":[],"x":NaN}', b'{"models":[],"x":Infinity}',
    b'{"models":[],"x":1e999}', b"\xff", b"[" * 2000 + b"]" * 2000])
def test_rejects_invalid_or_ambiguous_json(raw):
    with pytest.raises(DiscoveryError):
        parse_models(raw)


@pytest.mark.parametrize("changes", [dict(key=""), dict(key=" secret "), dict(key="x\n"),
    dict(key="\ud800"), dict(key="x\u202e"), dict(key="x" * 1025), dict(type=""),
    dict(type=7), dict(display_name="x" * 257), dict(display_name=None),
    dict(size_bytes=True), dict(size_bytes=-1), dict(size_bytes=1.2),
    dict(size_bytes=2**63), dict(max_context_length=False), dict(max_context_length=0),
    dict(max_context_length=2**31), dict(capabilities=None), dict(capabilities=[]),
    dict(capabilities={"vision": "true"}), dict(capabilities={"trained_for_tool_use": 1}),
    dict(loaded_instances=None), dict(loaded_instances=[None]),
    dict(loaded_instances=[dict(id="instance", config=None)]),
    dict(loaded_instances=[dict(id="instance", config={"context_length": -1})]),
    dict(loaded_instances=[dict(id="instance", config={"context_length": None})]),
    dict(loaded_instances=[dict(id="instance", config={"parallel": True})]),
    dict(loaded_instances=[dict(id="instance", config={"flash_attention": 1})]),
    dict(loaded_instances=[dict(id="", config={})])])
def test_rejects_invalid_declared_fields(changes):
    with pytest.raises(DiscoveryError):
        parse_models(encode(model(**changes)))


def test_global_duplicate_keys_and_instances():
    for records in [(model(), model()),
        (model(loaded_instances=[instance(), instance()]),),
        (model(loaded_instances=[instance()]), model(key="other", loaded_instances=[instance()]))]:
        with pytest.raises(DiscoveryError):
            parse_models(encode(*records))


@pytest.mark.parametrize("value", [None, "{}", {}, bytearray(b'{}'), memoryview(b'{}')])
def test_explicit_bytes_only(value):
    with pytest.raises(DiscoveryError, match="^response_bytes_required$"):
        parse_models(value)


def test_exact_selection_never_falls_back():
    result = parse_models(encode(model(loaded_instances=[instance()])))
    for key in ("publisher", "PUBLISHER/example", "instance-A"):
        with pytest.raises(DiscoveryError, match="^model_not_found$"):
            result.model(key)
    with pytest.raises(DiscoveryError, match="^instance_not_found$"):
        result.loaded_instance("publisher/example")


def test_bounds_accept_edge_and_reject_excess():
    records = [model(key=f"model-{i}") for i in range(discovery.MAX_MODELS)]
    assert len(parse_models(encode(*records)).models) == discovery.MAX_MODELS
    with pytest.raises(DiscoveryError):
        parse_models(encode(*records, model(key="extra")))
    instances = [instance(f"instance-{i}") for i in range(discovery.MAX_INSTANCES)]
    assert len(parse_models(encode(model(loaded_instances=instances))).models[0].loaded_instances) == discovery.MAX_INSTANCES
    with pytest.raises(DiscoveryError):
        parse_models(encode(model(loaded_instances=instances + [instance("extra")])))
    raw = encode()
    assert parse_models(raw + b" " * (discovery.MAX_BYTES - len(raw))).models == ()
    with pytest.raises(DiscoveryError, match="^response_size_limit$"):
        parse_models(raw + b" " * discovery.MAX_BYTES)


def test_content_free_errors_even_for_decoder_failures():
    marker = "synthetic-credential-must-not-echo"
    raw = ('{"models":[],"' + marker + '":1,"' + marker + '":2}').encode()
    try:
        parse_models(raw)
    except DiscoveryError as exc:
        assert marker not in str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    else:
        pytest.fail("duplicate JSON must be rejected")


def test_parser_has_no_io_or_management_surface(monkeypatch):
    # Fail if any decoder path starts file/network/process/profile activity.
    import builtins
    import io
    import os
    import socket
    import subprocess
    import urllib.request
    source = ast.parse(inspect.getsource(discovery))
    def forbidden(*args, **kwargs):
        pytest.fail("metadata decoding attempted IO")
    for obj, attr in [(builtins, "open"), (io, "open"), (socket, "socket"),
                      (socket, "create_connection"), (subprocess, "Popen"),
                      (os, "system"), (urllib.request, "urlopen")]:
        monkeypatch.setattr(obj, attr, forbidden)
    assert parse_models(encode(model())).models[0].key == "publisher/example"
    with pytest.raises(DiscoveryError):
        parse_models(b"invalid")
    imports = {node.module for node in ast.walk(source) if isinstance(node, ast.ImportFrom)}
    assert imports == {"dataclasses", "core.generation_transport"}
    assert {alias.name for node in ast.walk(source) if isinstance(node, ast.Import)
            for alias in node.names} == {"math", "unicodedata"}
