"""Manager-owned caption operations in the existing admitted Python worker.

Package/model installation and suggestions remain with their canonical services.
This host owns only a finite contained invocation, review pin and its lifetime.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import threading
import time
from uuid import uuid4

from .manager_description_settings import FIELDS, _validate
from .training_source_io import _assert_unredirected, read_stable_training_source
from .training_process_windows import WindowsTrainingJob

MAX_REQUEST = 64 * 1024
MAX_RESULT = 128 * 1024
_OPERATIONS = frozenset({'status', 'install-runtime', 'download-model', 'suggest'})


def caption_settings(settings):
    values = {key: getattr(settings, key) for key in FIELDS}
    return _validate(values)


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@dataclass(frozen=True)
class CaptionWorkerBinding:
    source_root: Path
    worker_root: Path
    interpreter: Path
    interpreter_sha256: str
    python_version: str
    profile_root: Path


class ManagerCaptionRuntime:
    def __init__(self, binding, check_binding, *, native_factory=WindowsTrainingJob):
        self.binding = binding
        self._check_binding = check_binding
        self._native_factory = native_factory
        self._lock = threading.Lock()
        self._active = False
        self._held = None

    @property
    def close_held(self):
        if self._held is not None:
            native, reader = self._held
            try:
                stopped = native.poll() is not None and native.active() == 0 and (reader is None or not reader.is_alive())
            except (OSError, RuntimeError):
                stopped = False
            if stopped:
                native.close()
                self._held = None
                self._active = False
                self._lock.release()
        return self._active

    def _inventory_pin(self):
        path = self.binding.profile_root / 'data/optional-components/installed-components.json'
        _assert_unredirected(path)
        if not path.exists():
            return None
        raw, _ = read_stable_training_source(path, max_bytes=16 * 1024 * 1024)
        return sha256(raw).hexdigest()

    def _review_token(self, settings):
        return sha256(_json({'settings': caption_settings(settings), 'inventory': self._inventory_pin(),
                            'worker': self.binding.interpreter_sha256,
                            'profile': str(self.binding.profile_root)})).hexdigest()

    def status(self, settings, *, cancelled=lambda: False, progress=None):
        value = self._request('status', settings, cancelled=cancelled, progress=progress)
        if (not isinstance(value, dict) or type(value.get('ready')) is not bool
                or not isinstance(value.get('components'), list) or len(value['components']) > 32
                or not isinstance(value.get('runtime_issue'), str) or len(value['runtime_issue']) > 4096
                or any(not isinstance(row, dict) or not isinstance(row.get('name'), str)
                       or len(row['name']) > 512 or type(row.get('installed')) is not bool
                       for row in value['components'])):
            raise ValueError('The caption worker returned invalid readiness data.')
        value['review_token'] = self._review_token(settings)
        return value

    def install_runtime(self, settings, review_token, *, cancelled=lambda: False, progress=None):
        return self._request('install-runtime', settings, review_token=review_token,
                             cancelled=cancelled, progress=progress)

    def download_model(self, settings, review_token, *, cancelled=lambda: False, progress=None):
        return self._request('download-model', settings, review_token=review_token,
                             cancelled=cancelled, progress=progress)

    def suggest(self, image_path, settings, *, selected_kinds, cancelled=lambda: False):
        from .generation_image_suggestions import ImageSetupProposal, ImageSetupSuggestionResult, PART_KINDS
        value = self._request('suggest', settings, image_path=str(image_path),
                              selected_kinds=list(selected_kinds), cancelled=cancelled)
        if not isinstance(value, dict) or set(value) != {'proposals', 'notes'}:
            raise ValueError('The caption worker returned an invalid suggestion result.')
        rows, notes = value['proposals'], value['notes']
        if not isinstance(rows, list) or len(rows) > 5 or not isinstance(notes, list) or len(notes) > 8:
            raise ValueError('The caption worker exceeded its suggestion scope.')
        proposals = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'kind', 'text', 'source_label', 'model_id'}
                    or row['kind'] not in PART_KINDS
                    or any(not isinstance(row[k], str) or not row[k].strip() or len(row[k]) > limit
                           for k, limit in [('text', 4096), ('source_label', 512), ('model_id', 256)])):
                raise ValueError('The caption worker returned an invalid proposal.')
            proposals.append(ImageSetupProposal(**row))
        if any(not isinstance(note, str) or len(note) > 4096 for note in notes):
            raise ValueError('The caption worker returned invalid notes.')
        return ImageSetupSuggestionResult(tuple(proposals), tuple(notes))

    @staticmethod
    def _drain(stream, messages):
        pending = b''
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if len(line) > 8192 or not line.startswith(b'CG_CAPTION_PROGRESS '):
                        continue
                    try:
                        value = json.loads(line[len(b'CG_CAPTION_PROGRESS '):])
                        if (isinstance(value, dict) and type(value.get('current')) is int and type(value.get('total')) is int
                                and isinstance(value.get('message'), str)):
                            # Keep the pipe draining even when UI progress is slower.
                            try: messages.put_nowait(value)
                            except queue.Full: pass
                    except (ValueError, TypeError):
                        pass
                if len(pending) > 8192:
                    pending = b''
        finally:
            pass  # EOF is observed by joining this retained reader.

    def _request(self, operation, settings, *, review_token=None, cancelled=lambda: False,
                 progress=None, image_path=None, selected_kinds=None):
        if operation not in _OPERATIONS:
            raise ValueError('Unsupported caption operation.')
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Finish the current caption operation before starting another.')
        self._active = True
        native = reader = None
        settled = False
        started = time.monotonic()
        maximum = 120 if operation == 'status' else 1800 if operation == 'suggest' else 7200
        def check():
            if cancelled():
                raise InterruptedError('Caption operation cancelled.')
            if time.monotonic() - started > maximum:
                raise TimeoutError('The caption operation exceeded its time limit.')
        try:
            check()
            self._check_binding(check)
            b = self.binding
            for path in [b.source_root, b.worker_root, b.interpreter, b.profile_root]:
                _assert_unredirected(path)
            if operation in {'install-runtime', 'download-model'}:
                if not isinstance(review_token, str) or review_token != self._review_token(settings):
                    raise RuntimeError('Caption setup changed. Check readiness and review it again.')
            payload = {'schema_version': 1, 'operation': operation,
                       'source_root': str(b.source_root), 'worker_root': str(b.worker_root),
                       'interpreter_sha256': b.interpreter_sha256, 'python_version': b.python_version,
                       'profile_root': str(b.profile_root), 'settings': caption_settings(settings)}
            if operation == 'suggest':
                payload.update(image_path=image_path, selected_kinds=selected_kinds)
            raw = _json(payload)
            if len(raw) > MAX_REQUEST:
                raise ValueError('Caption request exceeds its bound.')
            home = b.profile_root / 'operations'
            _assert_unredirected(home)
            home.mkdir(parents=True, exist_ok=True)
            directory = home / uuid4().hex
            directory.mkdir()
            request, result = directory / 'request.json', directory / 'result.json'
            with request.open('xb') as stream:
                stream.write(raw)
            script = b.source_root / 'core/manager_caption_worker.py'
            _assert_unredirected(script)
            if not script.is_file():
                raise RuntimeError('This Manager package is missing its caption worker source.')
            command = (str(b.interpreter), '-I', '-B', '-u', str(script), '--request', str(request),
                       '--request-sha256', sha256(raw).hexdigest(), '--result', str(result))
            environment = {k: v for k, v in os.environ.items()
                           if not k.upper().startswith(('PYTHON', 'PIP_'))}
            native = self._native_factory('CollectiGen.Caption.' + uuid4().hex)
            native.spawn_suspended(command, str(directory), environment)
            messages = queue.Queue(maxsize=64)
            reader = threading.Thread(target=self._drain, args=(native.stdout, messages), daemon=True)
            reader.start()
            check()
            self._check_binding(check)
            native.resume()
            while native.poll() is None or native.active() != 0:
                check()
                try: message = messages.get(timeout=.05)
                except queue.Empty: continue
                if message is not None and progress is not None:
                    progress(min(2147483647, max(0, message['current'])),
                             min(2147483647, max(0, message['total'])), message['message'][:4096])
            reader.join(timeout=3)
            if reader.is_alive():
                raise RuntimeError('Caption output is still settling; retain the original operation.')
            settled = True
            self._check_binding(check)
            check()
            if not result.is_file():
                raise RuntimeError('The caption worker stopped without a result. Review setup before retrying.')
            data, _ = read_stable_training_source(result, max_bytes=MAX_RESULT)
            reply = json.loads(data)
            if not isinstance(reply, dict) or reply.get('schema_version') != 1 or type(reply.get('ok')) is not bool:
                raise ValueError('The caption worker returned an invalid response.')
            if not reply['ok']:
                if reply.get('cancelled') is True:
                    raise InterruptedError('Caption operation cancelled.')
                raise RuntimeError(str(reply.get('error') or 'Caption operation failed.')[:4096])
            if native.poll() != 0:
                raise RuntimeError('The caption worker did not finish normally.')
            # Do not retain selected-image paths/settings in successful transient requests.
            request.unlink()
            result.unlink()
            directory.rmdir()
            return reply['value']
        finally:
            if native is not None and not settled:
                # A failed native creation with no retained process and no Job members
                # is authoritative non-dispatch, not an uncertain live child.
                if reader is None and getattr(native, 'process', None) is None and native.active() == 0:
                    settled = True
            if native is not None and not settled:
                try:
                    if 'directory' in locals():
                        (directory / 'cancel.flag').touch(exist_ok=True)
                    deadline = time.monotonic() + 1
                    while native.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    if native.poll() is None or native.active() != 0:
                        native.terminate()
                    deadline = time.monotonic() + 15
                    while (native.poll() is None or native.active() != 0) and time.monotonic() < deadline:
                        time.sleep(.05)
                    if reader is not None and reader.ident is not None:
                        reader.join(timeout=3)
                    settled = (native.poll() is not None and native.active() == 0
                               and (reader is None or not reader.is_alive()))
                finally:
                    if not settled:
                        self._held = (native, reader)
            if native is not None and settled:
                native.close()
            if native is None or settled:
                self._active = False
                self._lock.release()
