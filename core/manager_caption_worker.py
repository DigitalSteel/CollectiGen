"""Finite Manager caption child; canonical services own setup and inference.

Only the standard library is imported until the reviewed worker and explicit
component-only profile have been admitted. This is not a second installer.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 128 * 1024
PROGRESS_PREFIX = "CG_CAPTION_PROGRESS "
OPERATIONS = frozenset({"status", "install-runtime", "download-model", "suggest"})
REQUIRED_KEYS = frozenset({"schema_version", "operation", "source_root", "worker_root",
                           "interpreter_sha256", "python_version", "profile_root", "settings"})


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate request field.")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid JSON number.")))


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pin(value):
    return (type(value) is str and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def absolute_path(value, *, directory=False, must_exist=False):
    if type(value) is not str or not value or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("An absolute unredirected path is required.")
    path = Path(value)
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if must_exist and item == path:
                raise ValueError("Required path is unavailable.") from None
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Redirected paths are not allowed.")
        if (item != path or directory) and not stat.S_ISDIR(info.st_mode):
            raise ValueError("A directory is required.")
    if path.resolve(strict=False) != path:
        raise ValueError("Redirected paths are not allowed.")
    return path


def validate_request(payload, *, source_root, executable, python_version):
    """Admit the finite envelope without importing storage or model services."""
    if (type(payload) is not dict or not REQUIRED_KEYS <= payload.keys()
            or not payload.keys() <= REQUIRED_KEYS | {"image_path", "selected_kinds"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or type(payload["operation"]) is not str or payload["operation"] not in OPERATIONS):
        raise ValueError("Unsupported caption request schema or operation.")
    source = absolute_path(payload["source_root"], directory=True, must_exist=True)
    if source != Path(source_root):
        raise ValueError("Caption source binding changed.")
    worker = absolute_path(payload["worker_root"], directory=True, must_exist=True)
    profile = absolute_path(payload["profile_root"], directory=True)
    if any(profile == root or profile in root.parents or root in profile.parents for root in (source, worker)):
        raise ValueError("Caption profile must be separate from source and worker files.")
    interpreter = absolute_path(str(executable), must_exist=True)
    if (worker not in interpreter.parents or not interpreter.is_file()
            or not _pin(payload["interpreter_sha256"])
            or _digest(interpreter) != payload["interpreter_sha256"]
            or type(payload["python_version"]) is not str or payload["python_version"] != python_version):
        raise ValueError("Reviewed caption interpreter changed.")
    if type(payload["settings"]) is not dict:
        raise ValueError("Caption settings object is required.")
    if payload["operation"] == "suggest":
        absolute_path(payload.get("image_path"), must_exist=True)
        kinds = payload.get("selected_kinds")
        if (type(kinds) is not list or not kinds or len(kinds) > 5
                or any(type(kind) is not str or kind not in {"character", "scene", "pose", "style", "other"} for kind in kinds)
                or len(set(kinds)) != len(kinds)):
            raise ValueError("Distinct supported suggestion goals are required.")
    elif "image_path" in payload or "selected_kinds" in payload:
        raise ValueError("Image fields require an explicit suggestion operation.")
    return source, worker, profile


def validate_settings(values, fields, validator, settings_type):
    if type(values) is not dict or set(values) != set(fields):
        raise ValueError("Caption settings must contain exactly the approved controls.")
    validator(values)
    return settings_type(**values)


def reviewed_pip_command(source, worker, executable, python_version):
    pins_path = absolute_path(str(source / "tools" / "runtime-pins.json"), must_exist=True)
    with pins_path.open("rb") as stream:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("Runtime pins exceed the size limit.")
    pins = _json(raw)
    wheel = pins.get("pip_wheel", {}) if type(pins) is dict else {}
    name = wheel.get("filename")
    if (type(name) is not str or Path(name).name != name or "/" in name or "\\" in name
            or not name.endswith(".whl") or not _pin(wheel.get("sha256"))):
        raise ValueError("Reviewed pip wheel pin is invalid.")
    path = absolute_path(str(worker / name), must_exist=True)
    if not path.is_file() or _digest(path) != wheel["sha256"]:
        raise ValueError("The packaged pip wheel changed.")
    bootstrap = ("import runpy,sys; "
                 "assert sys.version.split()[0] == sys.argv.pop(1), 'Reviewed Python version changed'; "
                 "sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('pip', run_name='__main__')")
    return [str(executable), "-I", "-B", "-c", bootstrap, python_version, str(path)]


def bound_dependency_service(base, command, cancelled):
    """Change only stock pip bootstrapping; stock profile hashes stay authoritative."""
    class CaptionDependencyService(base):
        @classmethod
        def _acquire_optional_packages(cls, packages, site, stage, *, pip_command=None):
            check_cancelled(cancelled)
            if pip_command is not None and list(pip_command) != command:
                raise ValueError("Unexpected caption pip bootstrap override.")
            return super()._acquire_optional_packages(packages, site, stage, pip_command=list(command))
    return CaptionDependencyService


def check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError("Caption operation cancelled.")


def progress_writer(cancelled, stream):
    def progress(current, total, message):
        check_cancelled(cancelled)
        # Messages are supplied by canonical services, never by the request.
        text = str(message).encode("utf-8")[:2048].decode("utf-8", errors="ignore")
        row = {"current": int(current), "total": int(total), "message": text}
        raw = json.dumps(row, ensure_ascii=True, separators=(",", ":"))
        if len((PROGRESS_PREFIX + raw).encode("utf-8")) > 4096:
            row["message"] = text[:256]
            raw = json.dumps(row, ensure_ascii=True, separators=(",", ":"))
        stream.write(PROGRESS_PREFIX + raw + "\n")
        stream.flush()
    return progress


def dispatch(payload, settings, service, suggest, *, model_catalog, cancelled, progress):
    """Explicit service calls; suggestions retain canonical purpose prompting."""
    check_cancelled(cancelled)
    operation = payload["operation"]
    if operation in {"install-runtime", "download-model"}:
        hardware = service.component(service.JOY_HARDWARE_COMPONENT)
        if hardware is None or not hardware.installed:
            raise RuntimeError("The reviewed caption profile requires a supported NVIDIA GPU. Nothing was installed.")
    if operation == "status":
        model_id = settings.plain_language_model
        names = {*service.JOY_BASE_PACKAGES, service.JOY_HARDWARE_COMPONENT,
                 service.plain_language_component_name(model_id)}
        if settings.joycaption_quantization == "nf4" and model_id == "fancyfeast/llama-joycaption-beta-one-hf-llava":
            names.add("BitsAndBytes")
        ready = service.capability_ready("joy")
        rows = [asdict(row) for row in service.components(include_sizes=False) if row.name in names | {"Pillow", "BitsAndBytes"}]
        if len(rows) > 32 or any(type(value) is str and len(value.encode("utf-8")) > 4096
                                 for row in rows for value in row.values()):
            raise ValueError("Caption component metadata exceeds the display limit.")
        value = {"ready": ready, "components": rows,
                 "missing": [row["name"] for row in rows if row["name"] in names and not row["installed"]],
                 "runtime_issue": service.optional_profile_runtime_issue(), "model_id": model_id,
                 "model_display": model_catalog[model_id][0],
                 "model_directory": str(service.plain_language_model_dir_for(model_id))}
        if any(type(value[key]) is not str or len(value[key].encode("utf-8")) > 4096
               for key in ("runtime_issue", "model_id", "model_display", "model_directory")):
            raise ValueError("Caption status metadata exceeds the display limit.")
    elif operation == "install-runtime":
        value = {"message": service.install_profile("joy", progress=progress),
                 "cleanup_pending": bool(getattr(service, "optional_cleanup_pending", False))}
    elif operation == "download-model":
        value = {"message": service.setup_joycaption(download_model=True, model_id=settings.plain_language_model, progress=progress),
                 "cleanup_pending": bool(getattr(service, "optional_cleanup_pending", False))}
    elif operation == "suggest":
        value = asdict(suggest(payload["image_path"], settings,
                               selected_kinds=payload["selected_kinds"], cancelled=cancelled))
    else:
        raise ValueError("Unsupported caption operation.")
    check_cancelled(cancelled)
    return value


def encode_result(result):
    raw = (json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError("Caption result exceeds the size limit.")
    return raw


def publish_result(path, result):
    raw = encode_result(result)
    temporary = path.with_name(".caption-result-" + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    # Hard-link publication is atomic and refuses to replace an existing result.
    os.link(temporary, path)
    temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    request = absolute_path(args.request, must_exist=True)
    result = absolute_path(args.result)
    if request.parent != result.parent or request == result or result.exists():
        raise ValueError("A new result in the request operation directory is required.")
    cancelled = lambda: (request.parent / "cancel.flag").exists()
    try:
        check_cancelled(cancelled)
        with request.open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES or not _pin(args.request_sha256) or hashlib.sha256(raw).hexdigest() != args.request_sha256:
            raise ValueError("Caption request changed or exceeds the size limit.")
        payload = _json(raw)
        source, worker, profile = validate_request(payload, source_root=Path(__file__).absolute().parents[1],
                                                    executable=sys.executable, python_version=sys.version.split()[0])
        site = absolute_path(str(worker / "Lib" / "site-packages"), directory=True, must_exist=True)
        sys.path[:0] = [str(source), str(site)]
        from core.application_profile import bind_profile_root
        bind_profile_root(profile, program_root=source, initialize_defaults=False)
        from core.manager_description_settings import FIELDS, _validate
        from core.settings import AppSettings
        settings = validate_settings(payload["settings"], FIELDS, _validate, AppSettings)
        from core.dependencies import DependencyService
        from core.ai_interrogation import PLAIN_LANGUAGE_MODELS
        from core.generation_image_suggestions import suggest_image_setup_parts
        service_type = DependencyService
        if payload["operation"] in {"install-runtime", "download-model"}:
            command = reviewed_pip_command(source, worker, sys.executable, payload["python_version"])
            service_type = bound_dependency_service(DependencyService, command, cancelled)
        value = dispatch(payload, settings, service_type(settings), suggest_image_setup_parts,
                         model_catalog=PLAIN_LANGUAGE_MODELS, cancelled=cancelled,
                         progress=progress_writer(cancelled, sys.stdout))
        response = {"schema_version": 1, "ok": True, "value": value}
        encode_result(response)
    except Exception as exc:
        response = {"schema_version": 1, "ok": False,
                    "error": str(exc).encode("utf-8")[:4096].decode("utf-8", errors="ignore"),
                    "cancelled": isinstance(exc, InterruptedError) or cancelled()}
    publish_result(result, response)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
