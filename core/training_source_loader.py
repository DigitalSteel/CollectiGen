"""Load an exact owned Python source closure without consulting bytecode caches.

Workers must initially load this helper from its reviewed ``.py`` bytes with
``compile``/``exec`` (or ``runpy.run_path`` on that exact source file). Loading the
helper through an ordinary import first would leave the bootstrap cache gap.
The caller supplies approved locations and content receipts; this helper grants
no process, training, filesystem-discovery, or native-package authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType, ModuleType


MAX_SOURCE_BYTES = 4 * 1024**2
MAX_MODULES = 128
MAX_CLOSURE_BYTES = 32 * 1024**2
MAX_LOADED_MODULES = 10000
VERIFIER_MODULES = (
    'background_jobs', 'training_source_io', 'training_windows_file_identity',
    'training_process_windows', 'training_file_inventory', 'training_verification',
    'training_process', 'training_source_loader',
)
EXECUTION_MODULES = VERIFIER_MODULES + (
    'model_resource_registry', 'training_checkpoint', 'training_configuration',
    'training_conditioning_contract', 'training_conditioning_reference',
    'training_continuation', 'training_continuation_execution', 'training_continuation_hooks',
    'training_dataset_package_contract', 'training_dataset_preparation',
    'training_execution_dataset', 'training_execution_plan', 'training_execution_runtime',
    'training_image_effects_contract', 'training_loop_contract', 'training_model_contract',
    'training_model_loading_contract', 'training_model_pairing_contract',
    'training_package_effects', 'training_progress', 'training_reference_loading',
    'training_reference_tokenizers', 'training_restricted_loading_contract',
    'training_result', 'training_resume_adapter', 'training_resume_experiment',
    'training_runtime_contract', 'training_sdxl_loading', 'training_tokenizer_contract',
    'training_tokenizer_files', 'training_unet_step_contract',
)
WORKER_MODULES = {
    'execution': EXECUTION_MODULES,
    'readiness': EXECUTION_MODULES + ('training_execution_readiness',),
    'health': VERIFIER_MODULES + ('trainer_runtime_health', 'trainer_environment'),
    'verifier': VERIFIER_MODULES,
}
_PACKAGE = re.compile(r'_collectigen_[A-Za-z0-9_]{1,100}\Z')
_MODULE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}(?:\.[A-Za-z_][A-Za-z0-9_]{0,127}){0,15}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


class SourceLoaderError(ImportError):
    """Stable failure reasons; no selected paths or arbitrary source contents."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def strict_record(raw):
    def pairs(rows):
        value = {}
        for name, item in rows:
            if name in value: raise SourceLoaderError('owned_source_duplicate_field')
            value[name] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs)
    if canonical(value) != raw: raise SourceLoaderError('owned_source_record_changed')
    return value


def read_record(path, limit=4 * 1024**2):
    path = Path(path).absolute()
    before = _plain_path(path)
    if before[2] > limit: raise SourceLoaderError('owned_source_record_bound')
    with path.open('rb') as handle:
        if _state(os.fstat(handle.fileno())) != before:
            raise SourceLoaderError('owned_source_record_changed')
        raw = handle.read(limit + 1)
        after = _state(os.fstat(handle.fileno()))
    if after != before or _plain_path(path) != before or len(raw) != before[2]:
        raise SourceLoaderError('owned_source_record_changed')
    return strict_record(raw), raw


def reserved_request(path, *, command, work=None):
    """Bind bootstrap inputs to the parent's reservation before core imports.

    This is an early content check. The process service still reconstructs and
    validates the full current journal, native file identities and exact plan.
    """
    path, work = Path(path).absolute(), Path(work or Path.cwd()).absolute()
    value, raw = read_record(path)
    event, _ = read_record(work.parent / 'events/000.json')
    if (type(event) is not dict or set(event) != {'schema_version', 'sequence', 'previous', 'state', 'details'}
            or type(event['schema_version']) is not int or event['schema_version'] != 1
            or type(event['sequence']) is not int or event['sequence'] != 0
            or event['previous'] != '' or event['state'] != 'reserved'
            or type(event['details']) is not dict or set(event['details']) != {'plan', 'fingerprint'}):
        raise SourceLoaderError('owned_source_reservation_invalid')
    plan = event['details']['plan']
    if (type(plan) is not dict or hashlib.sha256(canonical(plan)).hexdigest() != event['details']['fingerprint']
            or type(plan.get('inputs')) is not list or not 1 <= len(plan['inputs']) <= 128
            or Path(plan.get('root', '')) / plan.get('name', '') / 'work' != work):
        raise SourceLoaderError('owned_source_reservation_changed')
    request_process = value.get('process', {})
    index = request_process.get('contract_input_index')
    fingerprint = hashlib.sha256(raw).hexdigest()
    if (type(index) is not int or not 0 <= index < len(plan['inputs'])
            or plan['inputs'][index] != {'sha256': fingerprint, 'size': len(raw)}
            or plan.get('preview_fingerprint') != fingerprint
            or plan.get('command_sha256') != hashlib.sha256(canonical(command)).hexdigest()):
        raise SourceLoaderError('owned_source_request_changed')
    requested = request_process.get('inputs')
    if type(requested) is not list or len(requested) + 1 != len(plan['inputs']):
        raise SourceLoaderError('owned_source_request_inputs_changed')
    seen = {index}
    for row in requested:
        index = row.get('index')
        if type(index) is not int or index in seen or not 0 <= index < len(plan['inputs']):
            raise SourceLoaderError('owned_source_request_inputs_changed')
        seen.add(index)
        state = row.get('state')
        if (type(state) is not list or len(state) != 5
                or {'sha256': row.get('sha256'), 'size': state[2]} != plan['inputs'][index]):
            raise SourceLoaderError('owned_source_request_inputs_changed')
    return value, requested


def worker_package(package_name, root, inputs, kind):
    """Install only the fixed closure, bound by the parent's exact input rows."""
    if kind not in WORKER_MODULES or type(inputs) is not list or not 1 <= len(inputs) <= 128:
        raise SourceLoaderError('owned_source_worker_inputs_invalid')
    root = Path(root).absolute()
    files = {}
    for name in WORKER_MODULES[kind]:
        path = root / (name + '.py')
        selected = [row for row in inputs if type(row) is dict and row.get('path') == str(path)]
        if len(selected) != 1 or type(selected[0].get('state')) not in (list, tuple) or len(selected[0]['state']) != 5:
            raise SourceLoaderError('owned_source_worker_input_missing')
        row = selected[0]
        files[name] = SourceFile(path, row.get('sha256'), row['state'][2])
    return OwnedSourcePackage(package_name, root, files)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    sha256: str
    size_bytes: int

    def __post_init__(self):
        path = Path(self.path)
        if (not path.is_absolute() or path != path.absolute() or path.suffix != '.py'
                or '..' in path.parts or '\x00' in str(path)
                or type(self.sha256) is not str or not _SHA.fullmatch(self.sha256)
                or type(self.size_bytes) is not int or not 0 <= self.size_bytes <= MAX_SOURCE_BYTES):
            raise SourceLoaderError('owned_source_binding_invalid')
        object.__setattr__(self, 'path', path)


def _state(value):
    # An in-operation stale guard, not a replacement for the process owner's
    # cross-interpreter filesystem identity contract or its contained checks.
    # Windows path/handle ctime can report different creation/change semantics.
    # Preserve complete identifiers rather than masking them to make a match.
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, getattr(value, 'st_file_attributes', 0)


def _plain_path(path, *, directory=False):
    try:
        for selected in (*reversed(path.parents), path):
            value = selected.lstat()
            if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
                raise SourceLoaderError('owned_source_redirected')
        if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
            raise SourceLoaderError('owned_source_type_invalid')
        if path.resolve(strict=True) != path:
            raise SourceLoaderError('owned_source_location_changed')
        return _state(value)
    except (OSError, RuntimeError):
        raise SourceLoaderError('owned_source_unavailable') from None


def _source(binding):
    """Read just the approved source; never inspect its cache or siblings."""
    before = _plain_path(binding.path)
    if before[2] != binding.size_bytes:
        raise SourceLoaderError('owned_source_changed')
    try:
        with binding.path.open('rb') as handle:
            if _state(os.fstat(handle.fileno())) != before:
                raise SourceLoaderError('owned_source_changed')
            raw = handle.read(binding.size_bytes + 1)
            if _state(os.fstat(handle.fileno())) != before:
                raise SourceLoaderError('owned_source_changed')
    except OSError:
        raise SourceLoaderError('owned_source_unavailable') from None
    if (len(raw) != binding.size_bytes or hashlib.sha256(raw).hexdigest() != binding.sha256
            or _plain_path(binding.path) != before):
        raise SourceLoaderError('owned_source_changed')
    return raw


class _SourceOnlyLoader(importlib.abc.Loader):
    def __init__(self, name, binding, *, owner=None, package=False):
        self.name, self.binding, self.owner, self.package = name, binding, owner, package

    def create_module(self, spec):
        return None

    def get_filename(self, fullname):
        if fullname != self.name: raise SourceLoaderError('owned_source_module_unapproved')
        return str(self.binding.path)

    def is_package(self, fullname):
        self.get_filename(fullname)
        return self.package

    def get_source(self, fullname):
        self.get_filename(fullname)
        return importlib.util.decode_source(_source(self.binding))

    def get_code(self, fullname):
        self.get_filename(fullname)
        # Deliberately not SourceFileLoader.get_code: that implementation can
        # execute a .pyc even with -B and a separately verified source receipt.
        return compile(_source(self.binding), str(self.binding.path), 'exec', dont_inherit=True, optimize=0)

    def exec_module(self, module):
        if module.__spec__.name != self.name or sys.modules.get(self.name) is not module:
            raise SourceLoaderError('owned_source_module_changed')
        if self.owner is not None:
            self.owner._loaded[self.name] = module
            self.owner._current()
        module.__cached__ = None
        try:
            exec(self.get_code(self.name), module.__dict__)
            _source(self.binding)
            if not _matches(module, self): raise SourceLoaderError('owned_source_module_changed')
            if self.owner is not None: self.owner._current()
        except BaseException:
            if self.owner is not None: self.owner._loaded.pop(self.name, None)
            raise


def _matches(module, loader):
    spec = getattr(module, '__spec__', None)
    return (sys.modules.get(loader.name) is module and getattr(module, '__loader__', None) is loader
            and getattr(module, '__file__', None) == str(loader.binding.path)
            and spec is not None and spec.name == loader.name and spec.loader is loader)


def _spec(name, loader):
    return importlib.util.spec_from_file_location(name, str(loader.binding.path), loader=loader,
        submodule_search_locations=[str(loader.binding.path.parent)] if loader.package else None)


def load_source_module(name, binding, *, expected_path):
    """Load a fixed bootstrap/helper or ``train_network`` patch from source.

    ``name`` and ``expected_path`` belong to the fixed caller, not an arbitrary
    request. A repeated identical load is allowed only for this loader's module;
    foreign/preloaded modules are never overwritten. Relative imports belong in
    ``OwnedSourcePackage`` instead.
    """
    if (type(name) is not str or not (_PACKAGE.fullmatch(name) or name == 'train_network')
            or not isinstance(binding, SourceFile) or binding.path != Path(expected_path)
            or not Path(expected_path).is_absolute()):
        raise SourceLoaderError('owned_source_module_unapproved')
    existing = sys.modules.get(name)
    if existing is not None:
        loader = getattr(existing, '__loader__', None)
        if (not isinstance(loader, _SourceOnlyLoader) or loader.owner is not None or loader.name != name
                or loader.binding != binding or not _matches(existing, loader)):
            raise SourceLoaderError('owned_source_module_occupied')
        _source(binding)
        return existing
    if name in sys.modules: raise SourceLoaderError('owned_source_module_occupied')
    loader = _SourceOnlyLoader(name, binding)
    module = importlib.util.module_from_spec(_spec(name, loader))
    sys.modules[name] = module
    try:
        loader.exec_module(module)
    except BaseException:
        if sys.modules.get(name) is module: del sys.modules[name]
        raise
    return module


class OwnedSourcePackage(importlib.abc.MetaPathFinder):
    """A fixed synthetic namespace with no fallback to unlisted modules.

    File keys are relative Python names, e.g. ``training_process`` or
    ``helpers.contract``. Every parent package needs an approved ``__init__.py``;
    the synthetic root itself executes no application ``core/__init__.py``.
    Imports outside this namespace use the ordinary import machinery unchanged.
    """
    def __init__(self, package_name, root, files):
        if type(package_name) is not str or not _PACKAGE.fullmatch(package_name):
            raise SourceLoaderError('owned_source_package_unapproved')
        self.name, self.root = package_name, Path(root)
        if not self.root.is_absolute() or self.root != self.root.absolute() or '..' in self.root.parts:
            raise SourceLoaderError('owned_source_location_unapproved')
        if not hasattr(files, 'items') or not 1 <= len(files) <= MAX_MODULES:
            raise SourceLoaderError('owned_source_closure_invalid')
        selected, packages = {}, set()
        for name, binding in files.items():
            if (type(name) is not str or not _MODULE.fullmatch(name) or '__init__' in name.split('.')
                    or not isinstance(binding, SourceFile)):
                raise SourceLoaderError('owned_source_closure_invalid')
            base = self.root.joinpath(*name.split('.'))
            if binding.path == base / '__init__.py': packages.add(name)
            elif binding.path != base.with_suffix('.py'):
                raise SourceLoaderError('owned_source_location_unapproved')
            selected[name] = binding
        if (sum(item.size_bytes for item in selected.values()) > MAX_CLOSURE_BYTES
                or any('.'.join(name.split('.')[:index]) not in packages
                       for name in selected for index in range(1, len(name.split('.'))))):
            raise SourceLoaderError('owned_source_closure_invalid')
        if any(not isinstance(key, str) for key in sys.modules) or len(sys.modules) > MAX_LOADED_MODULES:
            raise SourceLoaderError('owned_source_module_bound')
        if any(name == self.name or name.startswith(self.name + '.') for name in sys.modules):
            raise SourceLoaderError('owned_source_package_occupied')
        self.files, self._packages = MappingProxyType(selected), frozenset(packages)
        self._root_identity = _plain_path(self.root, directory=True)[:2]
        self._loaded, self._closed = {}, False
        package = ModuleType(self.name)
        package.__package__ = self.name
        package.__path__ = [str(self.root)]
        package.__loader__ = self
        package.__spec__ = importlib.machinery.ModuleSpec(self.name, self, is_package=True)
        package.__spec__.submodule_search_locations = package.__path__
        self._loaded[self.name] = package
        sys.modules[self.name] = package
        sys.meta_path.insert(0, self)

    def _current(self):
        if (self._closed or self not in sys.meta_path or len(sys.modules) > MAX_LOADED_MODULES
                or _plain_path(self.root, directory=True)[:2] != self._root_identity):
            raise SourceLoaderError('owned_source_package_changed')
        for name, module in tuple(sys.modules.items()):
            if type(name) is not str: raise SourceLoaderError('owned_source_module_bound')
            if name == self.name or name.startswith(self.name + '.'):
                if name not in self._loaded or self._loaded[name] is not module:
                    raise SourceLoaderError('owned_source_module_changed')
        root_module = self._loaded[self.name]
        if (sys.modules.get(self.name) is not root_module or root_module.__path__ != [str(self.root)]
                or root_module.__loader__ is not self):
            raise SourceLoaderError('owned_source_package_changed')

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.name and not fullname.startswith(self.name + '.'):
            return None
        self._current()
        relative = fullname.removeprefix(self.name + '.')
        if fullname == self.name or relative not in self.files:
            raise SourceLoaderError('owned_source_module_unapproved')
        parent = self.root.joinpath(*relative.split('.')[:-1])
        if path != [str(parent)] or target is not None:
            raise SourceLoaderError('owned_source_import_location_changed')
        return _spec(fullname, _SourceOnlyLoader(fullname, self.files[relative],
            owner=self, package=relative in self._packages))

    def import_module(self, name):
        self._current()
        if type(name) is not str or name not in self.files:
            raise SourceLoaderError('owned_source_module_unapproved')
        _source(self.files[name])
        module = importlib.import_module(self.name + '.' + name)
        self._current()
        loader = getattr(module, '__loader__', None)
        if (not isinstance(loader, _SourceOnlyLoader) or loader.owner is not self
                or loader.binding != self.files[name] or not _matches(module, loader)):
            raise SourceLoaderError('owned_source_module_changed')
        return module

    def close(self):
        if self._closed: return
        if self in sys.meta_path: sys.meta_path.remove(self)
        for name, module in sorted(self._loaded.items(), key=lambda pair: len(pair[0]), reverse=True):
            if sys.modules.get(name) is module: del sys.modules[name]
        self._closed = True

    def __enter__(self):
        self._current()
        return self

    def __exit__(self, *_):
        self.close()
