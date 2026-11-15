"""Generated invocation-only bearer custody; no sockets, profiles or secrets."""
import json
from dataclasses import replace

import pytest

from core.lmstudio_discovery import DiscoveryError, parse_models, refuse_credential_echo
from core.lmstudio_transport import DiscoveryInvocation, validate_discovery_token


def inventory():
    return parse_models(json.dumps({"models": [{"key": "generated/model", "type": "llm",
        "display_name": "Generated model", "size_bytes": 1,
        "loaded_instances": [{"id": "generated-instance", "config": {}}]}]}).encode())


@pytest.mark.parametrize("token", [None, "!", "Z" * 4096])
def test_exact_original_valid_grammar(token):
    assert validate_discovery_token(token) is token


@pytest.mark.parametrize("token", ["", " x", "x ", "x y", "x\n", "\x7f", "é", "x" * 4097, 1])
def test_invalid_grammar_before_callback(token):
    with pytest.raises(DiscoveryError, match="^invalid_token$"):
        DiscoveryInvocation("http://127.0.0.1:1234", token, lambda: pytest.fail(), lambda: False)


def test_anonymous_legacy_callable_and_one_use():
    calls = []
    def anonymous(endpoint, *, cancelled):
        calls.append(endpoint)
        return inventory()
    invocation = DiscoveryInvocation("http://127.0.0.1:1234", None, anonymous, lambda: False)
    assert invocation()[0] == "ok"
    assert invocation() == ("discovery_cancelled", None)
    assert len(calls) == 1


def test_valid_token_exact_handoff_and_disposal():
    marker = "generated-marker-7Q8Z"
    calls = []
    def discovery(endpoint, **kwargs):
        assert kwargs["token"] == marker and not kwargs["cancelled"]()
        calls.append(endpoint)
        return inventory()
    invocation = DiscoveryInvocation("http://127.0.0.1:1234", marker, discovery, lambda: False)
    assert invocation()[0] == "ok"
    assert invocation._token is None
    assert invocation()[0] == "discovery_cancelled" and len(calls) == 1


@pytest.mark.parametrize("error", [DiscoveryError, RuntimeError])
def test_arbitrary_errors_never_cross_worker_boundary(error):
    marker = "generated-error-marker-8Z9Q"
    def discovery(*args, **kwargs):
        raise error(marker)
    invocation = DiscoveryInvocation("http://127.0.0.1:1234", marker, discovery, lambda: False)
    result = invocation()
    assert result == ("error", None) and marker not in repr(result)
    assert invocation._token is None


def test_exception_stringification_is_never_invoked():
    class Unprintable(DiscoveryError):
        def __str__(self):
            raise RuntimeError("generated-stringifier-marker-9Z7Q")
    def discovery(*a, **kw):
        raise Unprintable("generated-exception-marker-9Z7Q")
    invocation=DiscoveryInvocation("http://127.0.0.1:1234", "generated-token-9Z7Q", discovery, lambda:False)
    assert invocation()==("error",None) and invocation._token is None


@pytest.mark.parametrize("field", ["key", "model_type", "display_name", "instance_id"])
def test_credential_echo_refuses_whole_observation(field):
    marker = "generated-echo-8Z9Q"
    value = inventory()
    model = value.models[0]
    if field == "instance_id":
        model = replace(model, loaded_instances=(replace(model.loaded_instances[0], instance_id=marker),))
    else:
        model = replace(model, **{field: marker})
    value = replace(value, models=(model,))
    invocation = DiscoveryInvocation("http://127.0.0.1:1234", marker,
                                     lambda *a, **kw: value, lambda: False)
    assert invocation() == ("credential_echo_refused", None)
    assert invocation._token is None
    assert refuse_credential_echo(value, None) is value  # Anonymous behavior unchanged.


@pytest.mark.parametrize("mode", ["discard", "cancelled"])
def test_queued_disposal_never_falls_back_to_anonymous_request(mode):
    calls = []
    invocation = DiscoveryInvocation("http://127.0.0.1:1234", "generated-queue-7Z9Q",
        lambda *a, **kw: calls.append(True), lambda: mode == "cancelled")
    if mode == "discard":
        invocation.discard()
    assert invocation() == ("discovery_cancelled", None)
    assert not calls and invocation._token is None
