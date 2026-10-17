"""Contained verification of reviewed trainer inputs; no model loading.

Selected filesystem I/O runs in a borrowed member of the trainer's Windows
Job. The controller waits only on memory and process handles, so a stuck
input read cannot prevent it from requesting whole-job termination.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import mmap
from pathlib import Path
import sys
import time
from uuid import uuid4


WORKER = Path(__file__).resolve().parents[1] / "tools" / "training_verification_worker.py"
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 4096
PHASES = ("before-create", "before-resume", "after-exit")


def verified_receipt_hashes(plan):
    """Validate all exact helper receipts without re-reading selected inputs."""
    from .training_process import TrainingProcessError, _bytes, _read, _sha
    hashes, nonces = [], set()
    for phase in PHASES:
        raw = _read(plan.destination / ("verification-" + phase + ".json"), MAX_RESPONSE)
        try:
            value = json.loads(raw)
            if type(value) is not dict or set(value) != {"schema_version", "nonce", "phase", "process_fingerprint", "request_sha256", "status"} or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["phase"] != phase or value["process_fingerprint"] != plan.fingerprint or value["status"] != "verified" or _bytes(value) != raw:
                raise ValueError("invalid_receipt")
            import re
            if type(value["nonce"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]) or value["nonce"] in nonces or type(value["request_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["request_sha256"]):
                raise ValueError("invalid_identity")
            expected_request = {"schema_version": 1, "nonce": value["nonce"], "phase": phase,
                "process_fingerprint": plan.fingerprint,
                "inputs": [asdict(item) for item in plan.inputs],
                "inventories": [asdict(item) for item in plan.inventories]}
            if value["request_sha256"] != _sha(_bytes(expected_request)):
                raise ValueError("unbound_request")
            nonces.add(value["nonce"])
            hashes.append(_sha(raw))
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise TrainingProcessError("The input verification receipts are invalid or stale.") from None
    return hashes


def verify_owned_inputs(plan, owner, phase, environment, cancel, deadline):
    from .training_process import TrainingProcessError, _bytes, _sha, _write_new

    if phase not in PHASES:
        raise TrainingProcessError("Unsupported process verification phase.")
    if cancel and cancel():
        raise InterruptedError("Process verification was cancelled.")
    if time.monotonic() >= deadline:
        raise TrainingProcessError("The shared input verification deadline expired.")
    nonce = uuid4().hex
    request = {"schema_version": 1, "nonce": nonce, "phase": phase,
        "process_fingerprint": plan.fingerprint,
        "inputs": [asdict(item) for item in plan.inputs],
        "inventories": [asdict(item) for item in plan.inventories]}
    raw = _bytes(request)
    if len(raw) > MAX_REQUEST:
        raise TrainingProcessError("The selected input verification request exceeds its bound.")
    expected = {"schema_version": 1, "nonce": nonce, "phase": phase,
        "process_fingerprint": plan.fingerprint, "request_sha256": _sha(raw)}
    mapping_name = "Local\\CollectiGen.Verify." + nonce
    # Private paths exist only in pagefile-backed memory. No request file,
    # selected-input names or command arguments are persisted in the receipt.
    # A length-prefixed reply follows the request. Read it only after the
    # helper and its descendants exit; never wait on synchronous pipe I/O.
    with mmap.mmap(-1, len(raw) + 4 + MAX_RESPONSE, tagname=mapping_name, access=mmap.ACCESS_WRITE) as mapping:
        mapping.write(raw)
        member = owner.member()
        baseline = owner.active()
        exited_at = None
        try:
            member.spawn_suspended((sys.executable, "-I", "-S", "-B", str(WORKER),
                mapping_name, str(len(raw)), expected["request_sha256"]),
                str(plan.destination / "work"), environment)
            if cancel and cancel():
                raise InterruptedError("Process verification was cancelled.")
            if time.monotonic() >= deadline:
                raise TrainingProcessError("The shared input verification deadline expired.")
            member.resume()
            while True:
                if cancel and cancel():
                    raise InterruptedError("Process verification was cancelled.")
                if time.monotonic() >= deadline:
                    raise TrainingProcessError("The shared input verification deadline expired.")
                code = member.poll()
                if code is not None:
                    exited_at = exited_at or time.monotonic()
                    if owner.active() == baseline:
                        break
                    if time.monotonic() - exited_at >= .5:
                        raise TrainingProcessError("The input verifier left an unverified process.")
                time.sleep(.01)
            size = int.from_bytes(mapping[len(raw):len(raw) + 4], "little")
            if not 0 < size <= MAX_RESPONSE:
                raise TrainingProcessError("The input verifier returned an invalid response size.")
            response = mapping[len(raw) + 4:len(raw) + 4 + size]
            try:
                value = json.loads(response)
                if type(value) is not dict or set(value) != set(expected) | {"status"} or any(
                    type(value[key]) is not type(wanted) or value[key] != wanted for key, wanted in expected.items()
                ) or _bytes(value) != bytes(response):
                    raise ValueError("unbound_result")
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise TrainingProcessError("The input verifier returned invalid or stale evidence.") from None
            if code != 0 or value["status"] != "verified":
                raise TrainingProcessError("Selected process inputs changed or could not be verified.")
            _write_new(plan.destination / ("verification-" + phase + ".json"), bytes(response))
        finally:
            # On failure the outer owner terminates and observes the entire
            # Job. Closing a borrowed member never closes the shared Job.
            member.close()
