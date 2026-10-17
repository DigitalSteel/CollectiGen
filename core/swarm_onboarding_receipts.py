"""Bounded append-only onboarding receipts, never a model or preset catalog."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .image_record import _atomic_create_text
from .training_source_io import _assert_unredirected
from .generation_transport import strict_json

MAX_STEPS = 65
MAX_BYTES = 256 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def validate_marker(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "operation_id", "source_digest", "account_fingerprint", "category", "destination", "before", "after"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or any(not isinstance(value.get(key), str) or not _HASH.fullmatch(value[key])
                   for key in ("operation_id", "source_digest", "account_fingerprint"))
            or not (value.get("after") is True or type(value.get("after")) is str and value["after"] in {"dark", "light"})):
        raise ValueError("Unsupported Swarm import receipt marker; review recovery before importing.")
    if value["category"] == "favorites":
        if (not isinstance(value["destination"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["destination"])
                or value["before"] is not None and type(value["before"]) is not bool or value["after"] is not True):
            raise ValueError("Unsupported favorite import marker.")
    elif value["category"] == "appearance":
        if (value["destination"] != "ui_theme" or value["before"] not in ("system", "dark", "light", "midnight")
                or value["after"] not in ("dark", "light")):
            raise ValueError("Unsupported appearance import marker.")
    else: raise ValueError("Unsupported onboarding marker category.")
    return deepcopy(value)


def validate_markers(values):
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("Swarm import history needs review before adding another import.")
    checked = [validate_marker(value) for value in values]
    if len({value["operation_id"] for value in checked}) != len(checked):
        raise ValueError("Swarm import history repeats an operation identity.")
    return checked


def _validate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("Choose between one and 65 bounded onboarding operations.")
    identities = set()
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"marker", "category", "destination", "before_digest"}:
            raise ValueError("Unsupported onboarding operation.")
        marker = validate_marker(step["marker"])
        if (step["category"] != marker["category"] or step["destination"] != marker["destination"]
                or step["before_digest"] != digest(marker["before"])):
            raise ValueError("Onboarding receipt differs from its destination marker.")
        if (step["category"] not in {"favorites", "appearance"}
                or not isinstance(step["destination"], str) or len(step["destination"]) > 80
                or not isinstance(step["before_digest"], str) or not _HASH.fullmatch(step["before_digest"])):
            raise ValueError("Unsupported onboarding destination.")
        if ((step["category"] == "favorites" and (not re.fullmatch(r"sha256:[0-9a-f]{64}", step["destination"]) or marker["after"] is not True))
                or (step["category"] == "appearance" and (step["destination"] != "ui_theme" or marker["after"] not in ("dark", "light")))):
            raise ValueError("Unsupported onboarding destination.")
        if marker["operation_id"] in identities: raise ValueError("Onboarding receipt repeats an operation identity.")
        identities.add(marker["operation_id"])


class OnboardingReceipts:
    """A durable plan precedes writes; destination markers reconcile interruption.

    Plans and per-step outcomes are no-clobber files. Incomplete plans are not
    execution authority. Resume always requires fresh source/destination review.
    """

    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    def _path(self, identity, suffix=""):
        if not isinstance(identity, str) or not _HASH.fullmatch(identity):
            raise ValueError("Invalid onboarding receipt identity.")
        path = self.directory / (identity + suffix + ".json")
        _assert_unredirected(path)
        return path

    @staticmethod
    def _read(path):
        _assert_unredirected(path)
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Onboarding receipt exceeds its bounded size.")
        return strict_json(raw)

    def _publish(self, path, value):
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        if len(encoded.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Onboarding receipt exceeds its bounded size.")
        _assert_unredirected(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        _assert_unredirected(path)
        try:
            _atomic_create_text(path, encoded)
        except FileExistsError:
            if self._read(path) != value:
                raise ValueError("An existing onboarding receipt differs; its contents were preserved.")
        if self._read(path) != value:
            raise OSError("Onboarding receipt read-back failed.")

    def create(self, steps):
        _validate_steps(steps)
        value = {"schema_version": 1, "steps": deepcopy(steps)}
        identity = digest(value)
        self._publish(self._path(identity), value)
        return identity

    def complete(self, identity, operation_id, outcome):
        plan = self.read(identity)
        if operation_id not in {step["marker"]["operation_id"] for step in plan["steps"]} or outcome not in {"completed", "already_completed", "local_correction_preserved"}:
            raise ValueError("Unsupported onboarding receipt outcome.")
        self._publish(self._path(identity, "." + operation_id), {"schema_version": 1,
            "operation_id": operation_id, "outcome": outcome})

    def read(self, identity):
        value = self._read(self._path(identity))
        if (not isinstance(value, dict) or set(value) != {"schema_version", "steps"}
                or value["schema_version"] != 1 or digest(value) != identity
                or not isinstance(value["steps"], list) or not 1 <= len(value["steps"]) <= MAX_STEPS):
            raise ValueError("Unsupported or changed onboarding receipt.")
        _validate_steps(value["steps"])
        return value

    def recent(self, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Use a bounded onboarding receipt page.")
        _assert_unredirected(self.directory)
        if not self.directory.exists(): return ()
        import os
        plans = []
        with os.scandir(self.directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 512: break
                if entry.name.endswith(".json") and _HASH.fullmatch(entry.name[:-5]) and entry.is_file(follow_symlinks=False):
                    plans.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name[:-5]))
        return tuple((identity, self.read(identity)) for _, identity in sorted(plans, reverse=True)[:limit])
