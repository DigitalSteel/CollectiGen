import base64
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest
from release_contract import ReleaseContractError, SIGNATURE_ALGORITHM, verify_rsa_pkcs1v15_sha256
from suite_core.installation import ComponentId
from suite_core.discovery.component_release_signature import (
    verify_component_release, verify_component_artifact, MAX_METADATA_BYTES,
)

FIXTURES = Path(__file__).parent / "fixtures"
KEY = json.loads((FIXTURES / "disposable_rsa_key.json").read_text(encoding="utf-8-sig"))
COMPONENT = ComponentId("collectigen.tag-encyclopedia")
ARTIFACT = b"INERT TEST ARTIFACT; NOT AN EXECUTABLE"
N = int.from_bytes(base64.urlsafe_b64decode(KEY["modulus"] + "=" * (-len(KEY["modulus"]) % 4)), "big")
D = int.from_bytes(base64.b64decode(KEY["private_exponent"]), "big")


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def manifest():
    return dict(schema_version=1, product="collectigen", component_id=COMPONENT.value,
        version="0.1.0-alpha.5", channel="beta", published_at="2026-10-06T00:00:00Z",
        supported_from="0.1.0-alpha.1", release_notes="Browse the tag encyclopedia offline.",
        platform=dict(os="linux", architecture="x86_64"), artifacts=[dict(
            name="reader-linux-x86_64.tar.gz", kind="component-package", size_bytes=len(ARTIFACT),
            sha256=sha256(ARTIFACT).hexdigest(), portable_manifest_sha256="a" * 64)])


def trust():
    return dict(schema_version=1, keys=[dict(key_id="disposable-test-only", algorithm=SIGNATURE_ALGORITHM,
        modulus=KEY["modulus"], exponent=KEY["exponent"], status="active")])


def signature(message):
    # Disposable fixture signing only. Shipping verifier reuses release_contract.
    size = (N.bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + sha256(message).digest()
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    signed = pow(int.from_bytes(encoded, "big"), D, N).to_bytes(size, "big")
    return dict(schema_version=1, algorithm=SIGNATURE_ALGORITHM, key_id="disposable-test-only",
        manifest_sha256=sha256(message).hexdigest(), signature=base64.b64encode(signed).decode())


def verify(m=None, s=None, t=None, **expected):
    message = raw(manifest() if m is None else m)
    defaults = dict(expected_component=COMPONENT, expected_version="0.1.0-alpha.5",
        expected_channel="beta", expected_os="linux", expected_architecture="x86_64")
    defaults.update(expected)
    return verify_component_release(message, raw(signature(message) if s is None else s),
        raw(trust() if t is None else t), **defaults)


def test_real_signature_and_pinned_fields():
    result = verify()
    assert result.component_id == COMPONENT and result.software_version == "0.1.0-alpha.5"
    assert (result.os, result.architecture, result.channel) == ("linux", "x86_64", "beta")
    assert result.artifact.sha256 == sha256(ARTIFACT).hexdigest()
    assert result.key_id == "disposable-test-only"
    assert result.manifest_sha256 == sha256(raw(manifest())).hexdigest()
    assert result.production_install_authorized is False


def test_independent_dotnet_pkcs1_vector():
    message = (FIXTURES / "dotnet_manifest.json").read_bytes()
    envelope = json.loads((FIXTURES / "dotnet_signature.json").read_text(encoding="utf-8-sig"))
    assert verify_rsa_pkcs1v15_sha256(message, base64.b64decode(envelope["signature"]), modulus=N, exponent=65537)
    assert signature(message)["signature"] == envelope["signature"]
    result = verify_component_release(message, raw(envelope), raw(trust()),
        expected_component=COMPONENT, expected_version="0.1.0-alpha.5", expected_channel="beta",
        expected_os="linux", expected_architecture="x86_64")
    assert result.production_install_authorized is False


@pytest.mark.parametrize("expected", [
    dict(expected_component=ComponentId("collectigen.suite-manager")),
    dict(expected_version="0.1.0-alpha.6"), dict(expected_channel="stable"),
    dict(expected_os="windows"), dict(expected_architecture="aarch64"),
])
def test_authenticated_wrong_identity_target_holds(expected):
    with pytest.raises(ReleaseContractError):
        verify(**expected)


def test_actual_artifact_hash_size_and_mutation(tmp_path):
    target = tmp_path / "inert.bin"
    target.write_bytes(ARTIFACT)
    result = verify()
    verify_component_artifact(target, result)
    target.write_bytes(b"X" + ARTIFACT[1:])
    with pytest.raises(ReleaseContractError, match="SHA-256"):
        verify_component_artifact(target, result)
    target.write_bytes(ARTIFACT + b"X")
    with pytest.raises(ReleaseContractError, match="size"):
        verify_component_artifact(target, result)


def test_exact_bytes_no_reserialization():
    original = raw(manifest())
    altered = original + b"\n"
    expected = dict(expected_component=COMPONENT, expected_version="0.1.0-alpha.5",
        expected_channel="beta", expected_os="linux", expected_architecture="x86_64")
    with pytest.raises(ReleaseContractError, match="digest"):
        verify_component_release(altered, raw(signature(original)), raw(trust()), **expected)
    verify_component_release(altered, raw(signature(altered)), raw(trust()), **expected)
    bom = b"\xef\xbb\xbf" + altered
    verify_component_release(bom, raw(signature(bom)), raw(trust()), **expected)


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(schema_version=True), lambda m: m.update(product="other"),
    lambda m: m.update(component_id="collectigen.suite-manager"),
    lambda m: m.update(version="0.1.0-alpha.05"), lambda m: m.update(channel="stable"),
    lambda m: m["platform"].update(architecture="aarch64"),
    lambda m: m.update(supported_from="0.2.0"), lambda m: m.update(unknown=1),
    lambda m: m["artifacts"][0].update(sha256="A" * 64),
    lambda m: m["artifacts"][0].update(portable_manifest_sha256="bad"),
    lambda m: m["artifacts"][0].update(size_bytes=True),
    lambda m: m["artifacts"][0].update(name=".."),
    lambda m: m["artifacts"][0].update(name="../escape"),
    lambda m: m["artifacts"][0].update(kind="portable-windows-x64"),
    lambda m: m["artifacts"].append(deepcopy(m["artifacts"][0])),
])
def test_resigned_malformed_or_wrong_component_metadata_holds(mutate):
    m = manifest()
    mutate(m)
    with pytest.raises(ReleaseContractError):
        verify(m=m)


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(keys=[]), lambda t: t["keys"][0].update(key_id="unknown"),
    lambda t: t["keys"][0].update(status="retired"),
    lambda t: t["keys"].append(deepcopy(t["keys"][0])),
    lambda t: t["keys"][0].update(exponent=True),
    lambda t: t["keys"][0].update(algorithm="none"),
    lambda t: t["keys"][0].update(modulus="!" * 342),
    lambda t: t["keys"][0].update(modulus="AQ"),
])
def test_unconfigured_unknown_inactive_duplicate_or_invalid_keys_hold(mutate):
    t = trust()
    mutate(t)
    with pytest.raises(ReleaseContractError):
        verify(t=t)


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(schema_version=True), lambda s: s.update(key_id="unknown"),
    lambda s: s.update(algorithm="none"), lambda s: s.update(signature="bad"),
    lambda s: s.update(signature=base64.b64encode(b"\0" * 256).decode()),
    lambda s: s.update(manifest_sha256="0" * 64), lambda s: s.update(unknown=1),
])
def test_signature_envelope_and_cryptographic_tamper_hold(mutate):
    s = signature(raw(manifest()))
    mutate(s)
    with pytest.raises(ReleaseContractError):
        verify(s=s)


@pytest.mark.parametrize("position,bad", [
    (0, b'{"schema_version":1,"schema_version":1}'),
    (1, b'{"signature":"x","signature":"y"}'),
    (2, b'{"schema_version":1,"keys":[],"keys":[]}'),
    (0, b'{"artifacts":[{"name":"x","name":"y"}]}'),
    (0, b'{"x":NaN}'), (0, b'\xff'), (0, b'[' * 100),
    (0, b' ' * (MAX_METADATA_BYTES + 1)),
], ids=["manifest-duplicate", "signature-duplicate", "trust-duplicate", "nested-duplicate", "nonfinite", "nonutf8", "deep", "oversized"])
def test_raw_metadata_holds(position, bad):
    message = raw(manifest())
    args = [message, raw(signature(message)), raw(trust())]
    args[position] = bad
    with pytest.raises(ReleaseContractError):
        verify_component_release(*args, expected_component=COMPONENT, expected_version="0.1.0-alpha.5",
            expected_channel="beta", expected_os="linux", expected_architecture="x86_64")


@pytest.mark.parametrize("schema", [1, 2])
def test_verification_does_not_read_trust_paths_or_write_or_network(monkeypatch, schema):
    import builtins
    import socket
    def deny(*a, **kw):
        raise AssertionError("verification is pure")
    monkeypatch.setattr(builtins, "open", deny)
    monkeypatch.setattr(Path, "read_bytes", deny)
    monkeypatch.setattr(Path, "write_bytes", deny)
    monkeypatch.setattr(socket, "socket", deny)
    assert (verify() if schema == 1 else verify_maturity()).production_install_authorized is False


@pytest.mark.parametrize("version", ["0.1.0-alpha.\u0665", "0.1\u0660.0-alpha.5"])
def test_ascii_canonical_versions_at_signed_and_expected_boundaries(version):
    m = manifest()
    m["version"] = version
    with pytest.raises(ReleaseContractError, match="exact version"):
        verify(m=m)
    with pytest.raises(ReleaseContractError, match="exact version"):
        verify(expected_version=version)


@pytest.mark.parametrize("position", [0, 1, 2])
def test_oversized_json_integer_in_all_metadata_inputs_holds(position):
    message = raw(manifest())
    args = [message, raw(signature(message)), raw(trust())]
    args[position] = b'{"ignored_number":' + b'9' * 5000 + b'}'
    with pytest.raises(ReleaseContractError, match="syntax"):
        verify_component_release(*args, expected_component=COMPONENT, expected_version="0.1.0-alpha.5",
            expected_channel="beta", expected_os="linux", expected_architecture="x86_64")


@pytest.mark.parametrize("size", [1.0, "123", -1, 0, 2**40 + 1])
def test_adjacent_artifact_numeric_admission(size):
    m = manifest()
    m["artifacts"][0]["size_bytes"] = size
    with pytest.raises(ReleaseContractError, match="artifact size"):
        verify(m=m)


def test_both_digest_fields_covered_by_exact_signature():
    original = raw(manifest())
    s = signature(original)
    for field in ("sha256", "portable_manifest_sha256"):
        m = manifest()
        m["artifacts"][0][field] = "b" * 64
        with pytest.raises(ReleaseContractError, match="digest"):
            verify(m=m, s=s)


def maturity_manifest(stage="alpha"):
    value = manifest()
    value.update(schema_version=2, version="0.4.0", supported_from="0.3.1",
        maturity=stage, channel="stable" if stage == "stable" else "beta")
    return value


def verify_maturity(m=None, **expected):
    value = maturity_manifest() if m is None else m
    defaults = dict(expected_version="0.4.0", expected_maturity=value.get("maturity"),
        expected_channel=value["channel"])
    defaults.update(expected)
    return verify(m=value, **defaults)


@pytest.mark.parametrize("stage", ["alpha", "beta", "stable"])
def test_explicit_signed_numeric_maturity_admission(stage):
    value = maturity_manifest(stage)
    result = verify_maturity(value)
    assert (result.schema_version, result.software_version, result.maturity) == (2, "0.4.0", stage)
    assert result.channel == value["channel"]
    assert result.manifest_sha256 == sha256(raw(value)).hexdigest()
    assert result.production_install_authorized is False
    # Existing calls carry no maturity expectation and must not gain schema2.
    with pytest.raises(ReleaseContractError, match="maturity"):
        verify(m=value, expected_version="0.4.0", expected_channel=value["channel"])


def test_default_root_and_main_app_verifier_do_not_admit_component_schema2():
    from release_contract import verify_release_manifest
    message = raw(maturity_manifest())
    with pytest.raises(ReleaseContractError, match="schema"):
        verify_release_manifest(message, raw(signature(message)), trust(), expected_channel="beta")
    old = verify()
    assert (old.schema_version, old.maturity) == (1, None)
    with pytest.raises(ReleaseContractError, match="schema1"):
        verify(expected_maturity="alpha")


@pytest.mark.parametrize("change", [
    {"schema_version": 3}, {"schema_version": True}, {"extra": "unknown"},
    {"version": "0.4.0-alpha.1"}, {"version": "a.0.4.0"}, {"version": "0.4.0+build1"},
    {"version": " 0.4.0"}, {"version": "0.4.\u0660"}, {"maturity": "Alpha"},
    {"maturity": None}, {"channel": "stable"}, {"supported_from": "0.4.1"},
    {"supported_from": "0.3.1-alpha.1"}, {"supported_from": "0.3.1+build1"},
    {"supported_from": " 0.3.1"}, {"product": "other"},
    {"component_id": "collectigen.suite-manager"},
])
def test_resigned_schema2_identity_envelope_refusals(change):
    value = maturity_manifest(); value.update(change)
    with pytest.raises(ReleaseContractError):
        verify_maturity(value)


@pytest.mark.parametrize("expected", [
    {"expected_maturity": None}, {"expected_maturity": "beta"}, {"expected_maturity": True},
    {"expected_channel": "stable"}, {"expected_version": "0.4.1"},
    {"expected_component": ComponentId("collectigen.suite-manager")},
    {"expected_os": "windows"}, {"expected_architecture": "aarch64"},
])
def test_schema2_exact_expectations_do_not_follow_signed_different_target(expected):
    with pytest.raises(ReleaseContractError):
        verify_maturity(**expected)


@pytest.mark.parametrize("field", ["maturity", "sha256", "portable_manifest_sha256"])
def test_schema2_signature_covers_maturity_and_both_artifact_pins(field):
    value = maturity_manifest(); signed = signature(raw(value))
    if field == "maturity":
        value[field] = "beta"
    else:
        value["artifacts"][0][field] = "b" * 64
    with pytest.raises(ReleaseContractError, match="digest"):
        verify(m=value, s=signed, expected_version="0.4.0", expected_maturity=value["maturity"])


@pytest.mark.parametrize("failure", ["key", "signature", "artifact_kind", "artifact_size", "trust"])
def test_schema2_reuses_signature_trust_and_artifact_guards(failure):
    value = maturity_manifest(); signed = signature(raw(value)); public = trust()
    if failure == "key": signed["key_id"] = "unknown"
    elif failure == "signature": signed["signature"] = base64.b64encode(b"\0" * 256).decode()
    elif failure == "artifact_kind": value["artifacts"][0]["kind"] = "portable-windows-x64"
    elif failure == "artifact_size": value["artifacts"][0]["size_bytes"] = True
    else: public["keys"][0]["status"] = "retired"
    with pytest.raises(ReleaseContractError):
        verify(m=value, s=signed, t=public, expected_version="0.4.0", expected_maturity="alpha")


def test_schema2_original_bytes_are_verified_without_reserialization():
    message = raw(maturity_manifest()) + b"\n"
    kwargs = dict(expected_component=COMPONENT, expected_version="0.4.0", expected_maturity="alpha",
        expected_channel="beta", expected_os="linux", expected_architecture="x86_64")
    with pytest.raises(ReleaseContractError, match="digest"):
        verify_component_release(message, raw(signature(message.rstrip())), raw(trust()), **kwargs)
    assert verify_component_release(message, raw(signature(message)), raw(trust()), **kwargs).schema_version == 2
