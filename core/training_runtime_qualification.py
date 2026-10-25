"""Reproducible, synthetic-only qualification of the pinned trainer parser.

No application launch consumes these reports. Package metadata identities are
not a complete environment lock, and parser success cannot qualify training.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from uuid import uuid4

from .training_process import (ProcessInput, TrainingProcessError, TrainingProcessService,
    TrainingRunJournal, _bytes, _directory, _read, _write_new)
from .training_runtime_contract import PYTHON_VERSIONS, SOURCE_REVISION, synthetic_configurations
from .training_file_inventory import FileInventoryError, SoftwareInventoryService, inspection_control
from .training_verification import verified_receipt_hashes
from .training_native_observation import NativeModuleObserver, verify_observation
from .training_tokenizer_contract import validate_report as validate_tokenizer_report
from . import training_selected_tokenizers as selected_tokens
from . import training_model_contract as model_layout
from . import training_tensor_contract as tensor_loading
from . import training_model_loading_contract as model_loading
from . import training_unet_step_contract as unet_step
from . import training_loop_contract as training_loop
from . import training_image_effects_contract as image_effects
from . import training_dataset_package_contract as dataset_package
from . import training_dataset_preparation as dataset_preparation
from . import training_model_pairing_contract as model_pairing_contract
from . import training_conditioning_contract as conditioning_contract
import io
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NATIVE_REMAINING_GATES = (
    'recipe_and_resource_admission', 'model_encoder_compatibility',
    'gpu_mixed_precision_and_unet', 'training_and_recovery',
    'output_verification_and_registration', 'application_setup_and_launch')
CATALOG = ROOT / "tools" / "training_runtime_sources.json"
PROBE = ROOT / "tools" / "training_runtime_probe.py"
CONTRACT = Path(__file__).with_name("training_runtime_contract.py")
TOKENIZER_CONTRACT = Path(__file__).with_name("training_tokenizer_contract.py")
MODEL_CONTRACT = Path(__file__).with_name('training_model_contract.py')
TENSOR_CONTRACT = Path(__file__).with_name('training_tensor_contract.py')
IMAGE_EFFECTS_CONTRACT = Path(__file__).with_name('training_image_effects_contract.py')
DATASET_PACKAGE_CATALOG = ROOT / 'tools/training_dataset_package.json'
DATASET_PACKAGE_BUNDLE = 'dataset-package-code.zip'
DATASET_PACKAGE_CODE = tuple(Path(__file__).with_name(name) for name in (
    'training_dataset_package_contract.py', 'training_package_effects.py', 'training_image_effects_contract.py'))


def dataset_package_bundle():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in [('__init__.py', b''), *((path.name, _read(path)) for path in DATASET_PACKAGE_CODE)]:
            info = zipfile.ZipInfo('_collectigen_dataset_probe/' + name, (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, raw)
    return stream.getvalue()


DATASET_PREPARATION_BUNDLE = 'dataset-preparation-code.zip'


def dataset_preparation_bundle():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        paths = (*DATASET_PACKAGE_CODE, Path(__file__).with_name('training_dataset_preparation.py'), Path(__file__).with_name('training_configuration.py'))
        for name, raw in [('__init__.py', b''), *((path.name, _read(path)) for path in paths)]:
            info = zipfile.ZipInfo('_collectigen_preparation/' + name, (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, raw)
    return stream.getvalue()


MODEL_LOADING_CATALOG = ROOT / 'tools/training_sdxl_layout.json'
MODEL_LOADING_BUNDLE = 'model-loading-code.zip'
MODEL_LOADING_CODE = tuple(Path(__file__).with_name(name) for name in (
    'training_model_loading_contract.py', 'training_sdxl_loading.py',
    'training_model_contract.py', 'model_resource_registry.py', 'training_source_io.py',
    'training_restricted_loading_contract.py'))


def model_loading_bundle():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in [('__init__.py', b''), *((path.name, _read(path)) for path in MODEL_LOADING_CODE)]:
            info = zipfile.ZipInfo('_collectigen_model_probe/' + name, (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, raw)
    return stream.getvalue()


UNET_STEP_BUNDLE = 'unet-step-code.zip'
UNET_STEP_CODE = (*MODEL_LOADING_CODE, Path(__file__).with_name('training_unet_step_contract.py'))


def unet_step_bundle():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in [('__init__.py', b''), *((path.name, _read(path)) for path in UNET_STEP_CODE)]:
            archive.writestr(zipfile.ZipInfo('_collectigen_unet_step/' + name, (1980, 1, 1, 0, 0, 0)), raw)
    return stream.getvalue()


def native_verified_flag(schema):
    return {14: 'generated_native_model_loading_verified',
        15: 'native_selected_tokenizer_behavior_verified',
        16: 'generated_unet_lora_step_verified', 17: 'generated_training_loop_verified'}[schema]


def verify_unet_outputs(checks, work):
    """Recheck only two bounded generated artifacts after their owner stops."""
    for filename, row in zip(unet_step.filenames(), checks['checks']):
        raw = _read(work / filename, limit=row['output_bytes'])
        if len(raw) != row['output_bytes'] or _sha(raw) != row['output_sha256']:
            raise ValueError('unet_step_retained_output_changed')


MODEL_PAIRING_BUNDLE = 'model-pairing-code.zip'
MODEL_PAIRING_CODE = tuple(dict.fromkeys((*MODEL_LOADING_CODE, *DATASET_PACKAGE_CODE,
    *(Path(__file__).with_name(name) for name in ('training_model_pairing_contract.py',
        'training_dataset_preparation.py', 'training_configuration.py', 'training_tokenizer_contract.py',
        'training_conditioning_contract.py', 'training_conditioning_reference.py')))))


def model_pairing_bundle():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in [('__init__.py', b''), *((path.name, _read(path)) for path in MODEL_PAIRING_CODE)]:
            archive.writestr(zipfile.ZipInfo('_collectigen_pairing/' + name, (1980, 1, 1, 0, 0, 0)), raw)
    return stream.getvalue()


TRAINING_LOOP_BUNDLE = 'training-loop-code.zip'
TRAINING_LOOP_CODE = tuple(dict.fromkeys((*MODEL_PAIRING_CODE, *UNET_STEP_CODE,
    Path(__file__).with_name('training_loop_contract.py'))))


def training_loop_bundle():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in [('__init__.py', b''), *((path.name, _read(path)) for path in TRAINING_LOOP_CODE)]:
            archive.writestr(zipfile.ZipInfo('_collectigen_training_loop/' + name, (1980, 1, 1, 0, 0, 0)), raw)
    return stream.getvalue()


def verify_loop_outputs(checks, work):
    """Recheck the three generated weights-only saves after their owner stops."""
    for row in checks['checkpoints']:
        raw = _read(work / row['filename'], limit=row['output_bytes'])
        if len(raw) != row['output_bytes'] or _sha(raw) != row['output_sha256']:
            raise ValueError('training_loop_retained_output_changed')


SELECTED_TOKENIZER_CODE = tuple(Path(__file__).with_name(name) for name in (
    'training_selected_tokenizers.py', 'training_tokenizer_files.py', 'training_source_io.py'))
SELECTED_TOKENIZER_BUNDLE = 'selected-tokenizer-code.zip'


def selected_tokenizer_bundle():
    """One deterministic, stored-code input keeps schema 5 within 128 inputs.

    No selected data or arbitrary archive enters this bundle. It contains only
    the four explicit project modules and an empty package initializer.
    """
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in [('__init__.py', b''), *((path.name, _read(path)) for path in (*SELECTED_TOKENIZER_CODE, TOKENIZER_CONTRACT))]:
            info = zipfile.ZipInfo('_collectigen_selected_probe/' + name, (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, raw)
    return stream.getvalue()


def _check_catalog_snapshot(inspection, inventory):
    selected={item.path:item for item in inspection.inputs}
    expected={}
    for relative in inspection.source_files:
        original=selected.get(str(Path(inspection.source)/relative))
        if original is None:
            raise TrainingProcessError('selected_tokenizer_source_catalog_changed')
        expected[relative]=(original.state[2],original.sha256)
    expected_directories={'.'}
    for relative in inspection.source_files:
        expected_directories.update(str(parent) for parent in PurePosixPath(relative).parents)
    directories={relative for relative,state in inventory.directories}
    if len(directories)!=len(inventory.directories) or directories!=expected_directories:
        raise TrainingProcessError('selected_tokenizer_source_catalog_changed')
    actual={relative:(state[2],digest) for relative,state,digest in inventory.files}
    if len(actual)!=len(inventory.files) or actual!=expected:
        raise TrainingProcessError('selected_tokenizer_source_catalog_changed')


def verify_selected_tokenizer_source_inventory(inspection, plan, request, *, should_cancel=None):
    """Bind the owned source tree to the exact inspected catalog."""
    if request.get('schema_version') not in {5,15}:
        return
    if len(plan.inventories)!=3:
        raise TrainingProcessError('selected_tokenizer_source_inventory_required')
    nonce=request.get('nonce')
    if not isinstance(nonce,str) or not re.fullmatch(r'[0-9a-f]{32}',nonce):
        raise TrainingProcessError('selected_tokenizer_source_inventory_authority')
    inputs=Path(plan.root)/('parser-inputs-'+nonce)
    source=inputs/'source';manifest=inputs/'source-inventory.json'
    binding=plan.inventories[2]
    if binding.root!=str(source) or binding.manifest_path!=str(manifest):
        raise TrainingProcessError('selected_tokenizer_source_inventory_authority')
    captured=next((item for item in plan.inputs if item.path==str(manifest)),None)
    if captured is None or captured.sha256!=binding.manifest_sha256:
        raise TrainingProcessError('selected_tokenizer_source_inventory_binding')
    if ProcessInput.capture(manifest)!=captured:
        raise TrainingProcessError('selected_tokenizer_source_inventory_changed')
    try:
        inventory=SoftwareInventoryService.load(source,manifest)
        if (inventory.fingerprint!=binding.manifest_sha256
                or len(inventory.files)!=binding.file_count
                or inventory.byte_count!=binding.byte_count
                or ProcessInput.capture(manifest)!=captured):
            raise TrainingProcessError('selected_tokenizer_source_inventory_changed')
        _check_catalog_snapshot(inspection,inventory)
        SoftwareInventoryService().verify(source,manifest,should_cancel=should_cancel)
        if ProcessInput.capture(manifest,should_cancel)!=captured:
            raise TrainingProcessError('selected_tokenizer_source_inventory_changed')
    except FileInventoryError as exc:
        raise TrainingProcessError('selected_tokenizer_source_inventory_changed') from exc


NATIVE_PROTOCOL = Path(__file__).with_name("training_native_protocol.py")
# Deliberately a prerequisite sample, not a resolved transitive dependency lock.
PACKAGES = {"accelerate": "1.6.0", "transformers": "4.54.1", "diffusers": "0.32.1",
    "toml": "0.10.2", "voluptuous": "0.15.2", "safetensors": "0.4.5",
    "huggingface-hub": "0.34.3", "einops": "0.7.0", "torch": None,
    "numpy": None, "tqdm": None, "pillow": None}
PROFILES = {"sd-scripts-requirements": PACKAGES,
    "kohya-uv-45088f04": {**PACKAGES, "accelerate": "1.8.1", "diffusers": "0.32.2"}}
SHA = re.compile(r"[0-9a-f]{64}")
PROBE_OPERATIONS = {"none", "socket.__new__", "socket.connect", "socket.bind",
    "socket.gethostname", "socket.gethostbyname", "socket.getaddrinfo", "socket.other",
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "pty.spawn"}


def native_connectivity_checks(schema):
    if type(schema) is not int or schema not in {14, 15, 16, 17}:
        raise ValueError('unsupported_native_probe')
    after = {14: 'loopback_after_loading', 15: 'loopback_after_tokenization', 16: 'loopback_after_optimizer', 17: 'loopback_after_training'}[schema]
    return {'environment_profile': 'local-processing-v1', 'offline_overrides_absent': True,
        'loopback_before_import': True, after: True}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate evidence key.")
            value[key] = item
        return value
    def invalid(_):
        raise ValueError("Non-finite evidence value.")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


@dataclass(frozen=True)
class RuntimeInspection:
    profile: str
    interpreter: str
    site_packages: str
    source: str
    blockers: tuple[str, ...]
    versions: tuple[tuple[str, str], ...]
    inputs: tuple[ProcessInput, ...]
    source_files: tuple[str, ...]
    catalog_sha256: str

    @property
    def fingerprint(self):
        return _sha(_bytes({"schema_version": 1, "source_revision": SOURCE_REVISION, "profile": self.profile,
            "interpreter": self.interpreter, "site_packages": self.site_packages,
            "source": self.source, "catalog_sha256": self.catalog_sha256,
            "blockers": self.blockers, "versions": self.versions,
            "inputs": [(item.path, item.state, item.sha256) for item in self.inputs]}))

    @property
    def probe_ready(self):
        return not self.blockers

    def summary(self):
        # No machine paths, exception text or package descriptions in reports.
        return {"schema_version": 1, "source_revision": SOURCE_REVISION,
            "profile": self.profile,
            "inspection_sha256": self.fingerprint, "probe_ready": self.probe_ready,
            "execution_ready": False, "blockers": list(self.blockers),
            "versions": dict(self.versions), "source_files_checked": len(self.source_files),
            "identity_scope": "selected_source_and_package_metadata_only",
            "runtime_code_verified": False,
            "remaining_gates": ["complete_runtime_code_identity", "offline_model_components",
                "model_compatibility", "exact_image_effects", "training_and_recovery",
                "output_verification_and_registration"]}


class TrainingRuntimeQualificationService:
    def inspect(self, source, environment, *, profile="sd-scripts-requirements", should_cancel=None):
        """Read only explicitly selected public source and software metadata.

        No discovery, imports, .pth execution, model/library/config inspection,
        installs, environment mutation or downloads occur here.
        """
        source, environment = Path(source).absolute(), Path(environment).absolute()
        if profile not in PROFILES:
            raise TrainingProcessError("Unsupported trainer prerequisite profile.")
        interpreter = environment / "Scripts" / "python.exe"
        site = environment / "Lib" / "site-packages"
        catalog_raw = _read(CATALOG)
        catalog = _json(catalog_raw)
        if type(catalog) is not dict or set(catalog) != {"schema_version", "source_revision", "files"} or type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1 or catalog["source_revision"] != SOURCE_REVISION:
            raise TrainingProcessError("Unsupported trainer source catalog.")
        entries = catalog["files"]
        if type(entries) is not dict or not 1 <= len(entries) <= 111:
            raise TrainingProcessError("Invalid trainer source inventory.")
        for relative, expected in entries.items():
            if type(relative) is not str or "\\" in relative or ":" in relative or PurePosixPath(relative).is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")) or type(expected) is not list or not 1 <= len(expected) <= 2 or any(type(item) is not str or not SHA.fullmatch(item) for item in expected):
                raise TrainingProcessError("Invalid trainer source identity.")
        blockers, inputs, versions, checked = [], [], [], []
        def capture(path):
            try:
                result = ProcessInput.capture(path, should_cancel)
                inputs.append(result)
                return result
            except (OSError, TrainingProcessError):
                return None
        if capture(interpreter) is None:
            blockers.append("interpreter_unavailable")
        for relative, expected in entries.items():
            record = capture(source / relative)
            if record is None or record.sha256 not in expected:
                blockers.append("source_missing_or_changed:" + relative)
            else:
                checked.append(relative)
        try:
            _directory(site)
        except (OSError, TrainingProcessError):
            blockers.append("package_directory_unavailable")
        else:
            for package, expected in PROFILES[profile].items():
                if should_cancel and should_cancel():
                    raise InterruptedError("Runtime inspection was cancelled.")
                # Match only the named public distributions; never execute their
                # code, entry points, editable finders or startup files.
                candidates = []
                for path in site.glob(package.replace("-", "_") + "-*.dist-info"):
                    candidates.append(path / "METADATA")
                    if len(candidates) > 1:
                        break
                if len(candidates) != 1:
                    blockers.append("package_missing_or_ambiguous:" + package)
                    continue
                path = candidates[0]
                record = capture(path)
                if record is None:
                    blockers.append("package_metadata_unavailable:" + package)
                    continue
                try:
                    raw = _read(path)
                    metadata = BytesParser().parsebytes(raw, headersonly=True)
                    names, values = metadata.get_all("Name", []), metadata.get_all("Version", [])
                    if _sha(raw) != record.sha256 or len(names) != 1 or _normalized(names[0]) != package or len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9.+!_-]{1,80}", values[0]):
                        raise ValueError("invalid_metadata")
                    version = values[0]
                except (OSError, TypeError, ValueError, TrainingProcessError):
                    blockers.append("package_metadata_invalid:" + package)
                    continue
                versions.append((package, version))
                if expected is not None and version != expected:
                    blockers.append("package_version_mismatch:" + package)
        return RuntimeInspection(profile, str(interpreter), str(site), str(source), tuple(blockers),
            tuple(versions), tuple(inputs), tuple(checked), _sha(catalog_raw))

    @staticmethod
    def inspect_software(inspection, python_base, *, should_cancel=None, progress=None):
        """Inventory two explicit software roots; never follow a configured path.

        pyvenv.cfg must agree with the separately supplied base selection. This
        check does not discover that base or assume its code is trusted.
        """
        if not inspection.probe_ready:
            raise TrainingProcessError("Resolve the selected runtime prerequisites first.")
        environment = Path(inspection.interpreter).parents[1]
        base = Path(python_base).absolute()
        _directory(base)
        if base.is_relative_to(environment) or environment.is_relative_to(base):
            raise TrainingProcessError("Choose distinct environment and base-Python software roots.")
        _directory(base / "Lib")
        ProcessInput.capture(base / "python.exe", should_cancel)
        fields = [line.split("=", 1)[1].strip() for line in _read(environment / "pyvenv.cfg").decode("utf-8").splitlines()
                  if "=" in line and line.split("=", 1)[0].strip() == "home"]
        if len(fields) != 1 or Path(fields[0]).absolute() != base:
            raise TrainingProcessError("The selected base Python differs from the environment's home.")
        service = SoftwareInventoryService()
        control = inspection_control(should_cancel)
        return tuple((role, service.capture(path, should_cancel=control, progress=progress))
            for role, path in (("environment", environment), ("python_base", base)))

    def run_parser(self, inspection, root, jobs, *, software=(), should_cancel=None, synthetic_tokenizers=False):
        """Execute a synthetic-only parser probe after exact inspection read-back.

        The caller owns authority for selected software imports. Actual model
        loading/training, environment repair and downloads are separate actions.
        """
        plan, request = self.prepare_parser(inspection, root, software=software, should_cancel=should_cancel,
                                            synthetic_tokenizers=synthetic_tokenizers)
        return self.execute_prepared(inspection, plan, request, jobs, software=software, should_cancel=should_cancel)

    def prepare_parser(self, inspection, root, *, software=(), should_cancel=None, synthetic_tokenizers=False,
                       selected_tokenizers=None, native_selected_tokenizers=False, synthetic_model_layout=False, synthetic_tensor_loading=False, synthetic_model_loading=False, native_model_loading=False, synthetic_image_effects=False, synthetic_dataset_package=False, selected_dataset=None, model_pairing=None, conditioning=False, synthetic_unet_step=False, synthetic_training_loop=False):
        """Blocking preparation primitive; the contained facade calls it in a helper."""
        if type(synthetic_unet_step) is not bool or (synthetic_unet_step and (not software or any((synthetic_training_loop, synthetic_tokenizers, native_selected_tokenizers, synthetic_model_layout, synthetic_tensor_loading, synthetic_model_loading, native_model_loading, synthetic_image_effects, synthetic_dataset_package, conditioning)) or any(value is not None for value in (selected_tokenizers, selected_dataset, model_pairing)))):
            raise TrainingProcessError('The generated GPU step requires full software selection and its own experiment.')
        if type(synthetic_training_loop) is not bool or (synthetic_training_loop and (not software or any((synthetic_unet_step, synthetic_tokenizers, native_selected_tokenizers, synthetic_model_layout, synthetic_tensor_loading, synthetic_model_loading, native_model_loading, synthetic_image_effects, synthetic_dataset_package, conditioning)) or any(value is not None for value in (selected_tokenizers, selected_dataset, model_pairing)))):
            raise TrainingProcessError('The generated GPU step requires full software selection and its own experiment.')
        if not inspection.probe_ready:
            raise TrainingProcessError("Resolve the reported runtime prerequisites before probing.")
        if type(synthetic_tokenizers) is not bool or (synthetic_tokenizers and not software):
            raise TrainingProcessError("The generated tokenizer probe requires full selected software inventories.")
        if type(synthetic_model_layout) is not bool or (synthetic_model_layout and (not software or synthetic_tokenizers or selected_tokenizers is not None)):
            raise TrainingProcessError('The meta-model probe requires full software selection and its own experiment.')
        if type(synthetic_tensor_loading) is not bool or (synthetic_tensor_loading and (not software or synthetic_tokenizers or synthetic_model_layout or selected_tokenizers is not None)):
            raise TrainingProcessError('The generated tensor probe requires full software selection and its own experiment.')
        if type(native_selected_tokenizers) is not bool or (native_selected_tokenizers and selected_tokenizers is None):
            raise TrainingProcessError('Native tokenizer qualification requires an exact selected pair.')
        if type(native_model_loading) is not bool or (native_model_loading and not synthetic_model_loading):
            raise TrainingProcessError("Native loading requires the full generated model experiment.")
        if type(synthetic_model_loading) is not bool or (synthetic_model_loading and (not software or synthetic_tokenizers or synthetic_model_layout or synthetic_tensor_loading or selected_tokenizers is not None)):
            raise TrainingProcessError('The full generated model probe requires full software selection and its own experiment.')
        if type(synthetic_image_effects) is not bool or (synthetic_image_effects and (not software or synthetic_tokenizers or synthetic_model_layout or synthetic_tensor_loading or synthetic_model_loading or selected_tokenizers is not None)):
            raise TrainingProcessError('The generated image probe requires full software selection and its own experiment.')
        if type(synthetic_dataset_package) is not bool or (synthetic_dataset_package and (not software or synthetic_image_effects or synthetic_tokenizers or synthetic_model_layout or synthetic_tensor_loading or synthetic_model_loading or selected_tokenizers is not None)):
            raise TrainingProcessError('The generated dataset-package probe requires full software selection and its own experiment.')
        dataset_selection = None
        if selected_dataset is not None:
            if not software or any((synthetic_tokenizers, synthetic_model_layout, synthetic_tensor_loading, synthetic_model_loading, synthetic_image_effects, synthetic_dataset_package)) or selected_tokenizers is not None or type(selected_dataset) is not dict or set(selected_dataset) != {'memory', 'binding', 'summary'}:
                raise TrainingProcessError('Dataset preparation requires its own full-software experiment.')
            dataset_selection = dataset_preparation.read_selection(selected_dataset['memory'])
            if dataset_preparation.binding(dataset_selection) != selected_dataset['binding']:
                raise TrainingProcessError('The selected package binding changed.')
            dataset_preparation.expected_report(selected_dataset)
        if model_pairing is not None:
            if selected_dataset is None:
                raise TrainingProcessError('Generated model pairing requires exact dataset preparation.')
            model_pairing_contract.expected_report(selected_dataset, model_pairing)
        if type(conditioning) is not bool or (conditioning and model_pairing is None):
            raise TrainingProcessError('Generated conditioning requires generated model pairing.')
        selection = None
        if selected_tokenizers is not None:
            if not software or type(selected_tokenizers) is not dict or set(selected_tokenizers) != {'memory', 'binding'}:
                raise TrainingProcessError('Selected tokenizer checks require full software inventories and exact binding.')
            selection = selected_tokens.read_selection(selected_tokenizers['memory'])
            if selected_tokens.binding(selection) != selected_tokenizers['binding']:
                raise TrainingProcessError('The selected tokenizer binding changed.')
            selected_tokens.inspect_selection(selection)
        current = self.inspect(inspection.source, Path(inspection.interpreter).parents[1], profile=inspection.profile, should_cancel=should_cancel)
        if current != inspection:
            raise TrainingProcessError("Runtime inspection changed; review current readiness first.")
        root = Path(root).absolute()
        _directory(root)
        if selection and any(root.is_relative_to(Path(row['path'])) for row in selection['slots']):
            raise TrainingProcessError('Keep probe work outside selected tokenizer directories.')
        if software:
            if tuple(role for role, _ in software) != ("environment", "python_base") or Path(software[0][1].root) != Path(inspection.interpreter).parents[1]:
                raise TrainingProcessError("The software inventories do not match the selected runtime.")
            if any(root.is_relative_to(Path(inventory.root)) for _, inventory in software):
                raise TrainingProcessError("Keep probe work outside the inventoried software.")
        if dataset_selection and (root.is_relative_to(Path(dataset_selection['root'])) or any(Path(dataset_selection['root']).is_relative_to(Path(inventory.root)) or Path(inventory.root).is_relative_to(Path(dataset_selection['root'])) for _, inventory in software)):
            raise TrainingProcessError('Keep dataset, software and work roots separate.')
        nonce = uuid4().hex
        inputs_root = root / ("parser-inputs-" + nonce)
        inputs_root.mkdir()  # fresh only; retain partial evidence on failure
        snapshot = inputs_root / "source"
        snapshot.mkdir()
        selected = {item.path: item for item in inspection.inputs}
        staged = []
        for relative in inspection.source_files:
            if should_cancel and should_cancel():
                raise InterruptedError("Runtime qualification was cancelled.")
            original = Path(inspection.source) / relative
            raw = _read(original)
            if _sha(raw) != selected[str(original)].sha256:
                raise TrainingProcessError("Trainer source changed before snapshotting.")
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_new(destination, raw)
            staged.append(destination)
        # Snapshot only explicitly catalogued public source, including its
        # license. Untracked files from the installation cannot join imports.
        for original in (PROBE, CONTRACT, *((NATIVE_PROTOCOL,) if software else ()),
                         *((TOKENIZER_CONTRACT,) if synthetic_tokenizers or selection else ()),
                         *((MODEL_CONTRACT,) if synthetic_model_layout else ()),
                         *((TENSOR_CONTRACT,) if synthetic_tensor_loading else ())):
            destination = inputs_root / original.name
            _write_new(destination, _read(original))
            staged.append(destination)
        if selection:
            destination = inputs_root / SELECTED_TOKENIZER_BUNDLE
            _write_new(destination, selected_tokenizer_bundle())
            staged.append(destination)
        if synthetic_model_loading:
            raw_catalog = _read(MODEL_LOADING_CATALOG, limit=model_layout.MAX_CATALOG)
            model_loading.checked_catalog(_json(raw_catalog))
            for name, raw in ((MODEL_LOADING_BUNDLE, model_loading_bundle()), (MODEL_LOADING_CATALOG.name, raw_catalog)):
                destination = inputs_root / name
                _write_new(destination, raw)
                staged.append(destination)
        if synthetic_unet_step:
            raw_catalog = _read(MODEL_LOADING_CATALOG, limit=model_layout.MAX_CATALOG)
            model_loading.checked_catalog(_json(raw_catalog))
            for name, raw in ((UNET_STEP_BUNDLE, unet_step_bundle()), (MODEL_LOADING_CATALOG.name, raw_catalog)):
                destination = inputs_root / name
                _write_new(destination, raw)
                staged.append(destination)
        if synthetic_training_loop:
            raw_catalog = _read(MODEL_LOADING_CATALOG, limit=model_layout.MAX_CATALOG)
            model_loading.checked_catalog(_json(raw_catalog))
            for name, raw in ((TRAINING_LOOP_BUNDLE, training_loop_bundle()), (MODEL_LOADING_CATALOG.name, raw_catalog)):
                destination = inputs_root / name
                _write_new(destination, raw)
                staged.append(destination)
        if synthetic_image_effects:
            destination = inputs_root / IMAGE_EFFECTS_CONTRACT.name
            _write_new(destination, _read(IMAGE_EFFECTS_CONTRACT))
            staged.append(destination)
        if synthetic_dataset_package:
            catalog_raw = _read(DATASET_PACKAGE_CATALOG, limit=512 * 1024)
            dataset_package.validate_catalog(_json(catalog_raw))
            for name, raw in ((DATASET_PACKAGE_BUNDLE, dataset_package_bundle()), (DATASET_PACKAGE_CATALOG.name, catalog_raw)):
                destination = inputs_root / name
                _write_new(destination, raw)
                staged.append(destination)
        dataset_inventory = None
        if dataset_selection:
            from .training_dataset_binding import prepare_replica
            replica = prepare_replica(dataset_selection, selected_dataset['summary'], inputs_root / 'dataset-package', should_cancel=should_cancel)
            dataset_inventory = SoftwareInventoryService().capture(replica, should_cancel=should_cancel)
            destination = inputs_root / DATASET_PREPARATION_BUNDLE
            _write_new(destination, dataset_preparation_bundle())
            staged.append(destination)
        request = {"schema_version": 1, "nonce": nonce, "source_revision": SOURCE_REVISION,
            "source": str(snapshot), "site_packages": inspection.site_packages,
            "contract": str(inputs_root / CONTRACT.name)}
        inventories = []
        if software:
            request.update(schema_version=3, runtime_roots={role: inventory.root for role, inventory in software},
                native_observation={"mapping": "Local\\CollectiGen.Native." + nonce,
                                    "protocol": str(inputs_root / NATIVE_PROTOCOL.name)})
            for role, inventory in software:
                destination = inputs_root / (role + "-inventory.json")
                SoftwareInventoryService.save(inventory, destination)
                inventories.append((inventory.root, destination))
        if selection:
            # The catalog is exact source authority, separate from the two
            # installed software roots. One manifest leaves bounded input room
            # for tokenizer files and the complete canonical verifier closure.
            source_inventory=SoftwareInventoryService().capture(snapshot,should_cancel=should_cancel)
            _check_catalog_snapshot(inspection,source_inventory)
            destination=inputs_root/'source-inventory.json'
            SoftwareInventoryService.save(source_inventory,destination)
            inventories.append((source_inventory.root,destination))
            staged=[path for path in staged if not path.is_relative_to(snapshot)]
        if synthetic_tokenizers:
            request.update(schema_version=4, tokenizer_contract=str(inputs_root / TOKENIZER_CONTRACT.name))
        if selection:
            request.update(schema_version=5, tokenizer_contract=str(inputs_root / TOKENIZER_CONTRACT.name),
                selected_tokenizers={**selected_tokenizers, 'code_bundle': str(inputs_root / SELECTED_TOKENIZER_BUNDLE)})
        if synthetic_model_layout:
            request.update(schema_version=6, model_contract=str(inputs_root / MODEL_CONTRACT.name))
        if synthetic_tensor_loading:
            request.update(schema_version=7, tensor_contract=str(inputs_root / TENSOR_CONTRACT.name))
        if synthetic_model_loading:
            request.update(schema_version=8, model_loading_bundle=str(inputs_root / MODEL_LOADING_BUNDLE),
                model_loading_catalog=str(inputs_root / MODEL_LOADING_CATALOG.name))
        if synthetic_image_effects:
            request.update(schema_version=9, image_effects_contract=str(inputs_root / IMAGE_EFFECTS_CONTRACT.name))
        if synthetic_dataset_package:
            request.update(schema_version=10, dataset_package_bundle=str(inputs_root / DATASET_PACKAGE_BUNDLE),
                dataset_package_catalog=str(inputs_root / DATASET_PACKAGE_CATALOG.name))
        if dataset_selection:
            destination = inputs_root / 'dataset-inventory.json'
            SoftwareInventoryService.save(dataset_inventory, destination)
            inventories.append((dataset_inventory.root, destination))
            request.update(schema_version=11, dataset_preparation={
                'package': str(inputs_root / 'dataset-package'), 'binding': selected_dataset['binding'],
                'summary': selected_dataset['summary'], 'code_bundle': str(inputs_root / DATASET_PREPARATION_BUNDLE)})
        if model_pairing is not None:
            raw_catalog = _read(MODEL_LOADING_CATALOG, limit=model_layout.MAX_CATALOG)
            model_loading.checked_catalog(_json(raw_catalog))
            for name, raw in ((MODEL_PAIRING_BUNDLE, model_pairing_bundle()), (MODEL_LOADING_CATALOG.name, raw_catalog)):
                destination = inputs_root / name
                _write_new(destination, raw)
                staged.append(destination)
            request.update(schema_version=12, model_pairing=model_pairing,
                model_pairing_bundle=str(inputs_root / MODEL_PAIRING_BUNDLE),
                model_pairing_catalog=str(inputs_root / MODEL_LOADING_CATALOG.name))
        if conditioning:
            request.update(schema_version=13, conditioning=True)
        request_path = inputs_root / "request.json"
        if native_model_loading:
            request.update(schema_version=14, environment_profile="local-processing-v1")
        if native_selected_tokenizers:
            request.update(schema_version=15, environment_profile="local-processing-v1")
        if synthetic_unet_step:
            request.update(schema_version=16, environment_profile='local-processing-v1',
                unet_step_bundle=str(inputs_root / UNET_STEP_BUNDLE),
                unet_step_catalog=str(inputs_root / MODEL_LOADING_CATALOG.name))
        if synthetic_training_loop:
            request.update(schema_version=17, environment_profile='local-processing-v1',
                training_loop_bundle=str(inputs_root / TRAINING_LOOP_BUNDLE),
                training_loop_catalog=str(inputs_root / MODEL_LOADING_CATALOG.name))
        raw_request = _bytes(request)
        _write_new(request_path, raw_request)
        metadata = [] if software else [Path(item.path) for item in inspection.inputs if item.path.endswith("METADATA")]
        process = TrainingProcessService()
        plan = process.prepare(root, "parser-run-" + nonce,
            (inspection.interpreter, "-I", "-S", "-B", *(('-X', 'utf8') if synthetic_training_loop else ()),
                str(inputs_root / PROBE.name), str(request_path)),
            inputs=(*staged, request_path, *metadata, *(selected_tokens.input_paths(selection) if selection else ())),
            inventories=inventories, preview_fingerprint=inspection.fingerprint,
            environment_profile="local-processing-v1" if native_model_loading or native_selected_tokenizers or synthetic_unet_step or synthetic_training_loop else "diagnostic-offline-v1",
            timeout_seconds=1200 if native_model_loading or synthetic_unet_step or synthetic_training_loop else 300 if synthetic_model_loading or dataset_selection else 90, log_limit=1024 * 1024, should_cancel=should_cancel)
        for item in plan.inputs:
            if item.path in selected and item != selected[item.path]:
                raise TrainingProcessError("Runtime inputs changed while preparing the probe.")
        verify_selected_tokenizer_source_inventory(inspection,plan,request,should_cancel=should_cancel)
        return plan, request

    def execute_prepared(self, inspection, plan, request, jobs, *, software=(), should_cancel=None, reinspect=None, preflight_receipts=(), process_service=None):
        """Execute exact prepared inputs; callers may supply contained reinspection."""
        if request.get('schema_version') in {14, 15, 16, 17} and (plan.schema_version != 3 or plan.environment_profile != 'local-processing-v1' or request.get('environment_profile') != plan.environment_profile or len(plan.inventories) != (3 if request['schema_version']==15 else 2)):
            raise TrainingProcessError('Native qualification requires its exact local process and software plan.')
        verify_selected_tokenizer_source_inventory(inspection,plan,request,should_cancel=should_cancel)
        process = process_service or TrainingProcessService()
        observer = NativeModuleObserver(request, software) if software else None
        try:
            result = process.execute(plan, plan.fingerprint, jobs,
                **({"running_observer": observer} if observer else {}),
                **({"should_cancel": should_cancel} if should_cancel else {}))
        finally:
            if observer is not None:
                observer.close()
        report = self.verify_result(inspection, plan, result, request)
        current = reinspect() if reinspect else self.inspect(inspection.source, Path(inspection.interpreter).parents[1], profile=inspection.profile, should_cancel=should_cancel)
        if current != inspection:
            report.update(status="held", reason="runtime_changed", checks=None)
            if request['schema_version'] in {4, 5, 15}:
                report.update(tokenizer_checks=None, synthetic_tokenizers_verified=False)
            if request['schema_version'] in {5, 15}:
                report.update(selected_tokenizer_checks=None, selected_tokenizer_behavior_verified=False)
            if request['schema_version'] == 6:
                report.update(model_layout_checks=None, synthetic_model_layout_verified=False)
            if request['schema_version'] == 7:
                report.update(tensor_loading_checks=None, synthetic_tensor_loading_verified=False)
            if request['schema_version'] in {8, 14}:
                report.update(model_loading_checks=None, synthetic_model_loading_verified=False)
            if request['schema_version'] == 9:
                report.update(image_effects_checks=None, synthetic_image_effects_verified=False)
            if request['schema_version'] == 10:
                report.update(dataset_package_checks=None, synthetic_dataset_package_verified=False)
            if request['schema_version'] in {11, 12, 13}:
                report.update(dataset_preparation_checks=None, package_preparation_verified=False)
            if request['schema_version'] in {12, 13}:
                report.update(model_pairing_checks=None, generated_model_pairing_verified=False)
            if request['schema_version'] == 13:
                report.update(conditioning_checks=None, generated_encoder_conditioning_verified=False)
            if request['schema_version'] == 16:
                report['unet_step_checks'] = None
            if request['schema_version'] == 17:
                report['training_loop_checks'] = None
        if request['schema_version'] in {14, 15, 16, 17}:
            flag = native_verified_flag(request['schema_version'])
            report[flag] = report['status'] == 'parser_checked'
            if report['status'] != 'parser_checked':
                report['local_processing_checks'] = None
        if software and report["status"] == "parser_checked":
            report.update(identity_scope="selected_source_python_and_environment_inventory",
                software_inventory_verified=True,
                software_inventories={role: inventory.summary() for role, inventory in software})
            # Module filenames at the parser barrier are matched to these
            # inventories. This does not measure loaded memory, later modules,
            # system files or drivers. Later native/GPU/training gates remain;
            # legacy reports retain their original readiness gate lists.
        else:
            report["software_inventory_verified"] = False
        if preflight_receipts:
            report['preflight_receipt_sha256'] = list(preflight_receipts)
        if plan.destination.is_dir():
            _write_new(plan.destination / "qualification.json", _bytes(report))
        return report, plan.destination

    @staticmethod
    def verify_result(inspection, plan, result, request):
        report = {**inspection.summary(), "status": "held", "reason": "process_incomplete",
            "process_status": result.status, "process_plan_sha256": plan.fingerprint,
            "journal_tip": result.journal_tip, "checks": None}
        if request['schema_version'] in {4, 5, 15}:
            report.update(tokenizer_checks=None, synthetic_tokenizers_verified=False)
        if request['schema_version'] in {5, 15}:
            report.update(selected_tokenizer_checks=None, selected_tokenizer_behavior_verified=False)
        if request['schema_version'] == 6:
            report.update(model_layout_checks=None, synthetic_model_layout_verified=False)
        if request['schema_version'] == 7:
            report.update(tensor_loading_checks=None, synthetic_tensor_loading_verified=False)
        if request['schema_version'] in {8, 14}:
            report.update(model_loading_checks=None, synthetic_model_loading_verified=False)
        if request['schema_version'] == 9:
            report.update(image_effects_checks=None, synthetic_image_effects_verified=False)
        if request['schema_version'] == 10:
            report.update(dataset_package_checks=None, synthetic_dataset_package_verified=False)
        if request['schema_version'] in {11, 12, 13}:
            report.update(dataset_preparation_checks=None, package_preparation_verified=False)
        if request['schema_version'] in {12, 13}:
            report.update(model_pairing_checks=None, generated_model_pairing_verified=False)
        if request['schema_version'] == 13:
            report.update(conditioning_checks=None, generated_encoder_conditioning_verified=False)
        if request['schema_version'] == 16:
            report.update(unet_step_checks=None, selected_training_started=False)
        if request['schema_version'] == 17:
            report.update(training_loop_checks=None, selected_training_started=False)
        if request['schema_version'] in {14, 15, 16, 17}:
            flag = native_verified_flag(request['schema_version'])
            report[flag] = False
            report.update(local_processing_checks=None,
                environment_profile='local-processing-v1',
                **({'selected_training_started': False} if request['schema_version'] in {16, 17} else {'training_started': False}),
                remaining_gates=list(NATIVE_REMAINING_GATES))
            if plan.schema_version != 3 or plan.environment_profile != 'local-processing-v1' or request.get('environment_profile') != plan.environment_profile or len(plan.inventories) != (3 if request['schema_version']==15 else 2):
                report['reason'] = 'native_process_profile_mismatch'
                return report
        if (result.status, result.exit_code) not in {("exited_unverified", 0), ("failed", 2)} or not result.tree_stopped or result.destination != plan.destination:
            return report
        try:
            verify_selected_tokenizer_source_inventory(inspection,plan,request)
            rows, tip = TrainingRunJournal(plan.destination).read()
            if tip != result.journal_tip or rows[0]["details"]["plan"] != json.loads(_bytes(TrainingProcessService._receipt(plan))) or rows[-1]["state"] != result.status or rows[-1]["details"].get("exit_code") != result.exit_code or rows[-1]["details"].get("tree_stopped") is not True:
                raise ValueError("process_receipt_mismatch")
            verification_hashes = verified_receipt_hashes(plan)
            if request['schema_version'] in {14, 15, 16, 17} and len(verification_hashes) != 3:
                raise ValueError('native_input_receipts_incomplete')
            if rows[-1]["details"].get("input_verification_sha256") != verification_hashes:
                raise ValueError("input_verification_mismatch")
            report["input_verification_sha256"] = verification_hashes
            raw = _read(plan.destination / "work" / "parser-result.json", limit=(64 if request['schema_version'] in {5, 15} else 32) * 1024)
            value = _json(raw)
            fields = {"schema_version", "nonce", "request_sha256", "source_revision", "status", "reason", "checks", "execution_ready", "python_version", "denied_operation", "failure_site", "blocked_operations", "software_roots_checked"}
            if request["schema_version"] in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
                fields.add("native_observation_sha256")
            if request['schema_version'] in {4, 5, 15}:
                fields.add('tokenizer_checks')
            if request['schema_version'] in {5, 15}:
                fields.add('selected_tokenizer_checks')
            if request['schema_version'] == 6:
                fields.add('model_layout_checks')
            if request['schema_version'] == 7:
                fields.add('tensor_loading_checks')
            if request['schema_version'] in {8, 14}:
                fields.add('model_loading_checks')
            if request['schema_version'] == 9:
                fields.add('image_effects_checks')
            if request['schema_version'] == 10:
                fields.add('dataset_package_checks')
            if request['schema_version'] in {11, 12, 13}:
                fields.add('dataset_preparation_checks')
            if request['schema_version'] in {12, 13}:
                fields.add('model_pairing_checks')
            if request['schema_version'] == 13:
                fields.add('conditioning_checks')
            if request['schema_version'] == 16:
                fields.add('unet_step_checks')
            if request['schema_version'] == 17:
                fields.add('training_loop_checks')
            if request['schema_version'] in {14, 15, 16, 17}:
                fields.update(('environment_profile', 'local_processing_checks'))
            if set(value) != fields or type(value["software_roots_checked"]) is not bool or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["nonce"] != request["nonce"] or value["request_sha256"] != _sha(_bytes(request)) or value["source_revision"] != SOURCE_REVISION or value["execution_ready"] is not False or type(value["python_version"]) is not str or not re.fullmatch(r"3\.\d{1,2}\.\d{1,2}", value["python_version"]) or type(value["denied_operation"]) is not str or value["denied_operation"] not in PROBE_OPERATIONS or type(value["failure_site"]) is not str or (value["failure_site"] and not re.fullmatch(r"(?:torch|accelerate|transformers|diffusers|huggingface_hub|numpy|tqdm|safetensors|toml|voluptuous|einops|PIL)(?:\.[A-Za-z0-9_]+)*:[0-9]{1,6}", value["failure_site"])):
                raise ValueError("parser_receipt_mismatch")
            if request['schema_version'] in {14, 15, 16, 17} and value['environment_profile'] != plan.environment_profile:
                raise ValueError('native_profile_mismatch')
            operations = value["blocked_operations"]
            if type(operations) is not list or len(operations) > 20 or any(type(item) is not str or item not in PROBE_OPERATIONS | {"external_write_denied", "external_or_model_read_denied", "external_directory_denied"} for item in operations) or len(set(operations)) != len(operations):
                raise ValueError("denial_evidence_invalid")
            report["blocked_operations"] = operations
            if result.exit_code == 2:
                if request['schema_version'] == 16 and value['unet_step_checks'] is not None:
                    raise ValueError('held_unet_step_receipt_invalid')
                if request['schema_version'] == 17 and value['training_loop_checks'] is not None:
                    raise ValueError('held_training_loop_receipt_invalid')
                if request['schema_version'] in {14, 15, 16, 17} and value['local_processing_checks'] is not None:
                    raise ValueError('held_native_connectivity_invalid')
                if request['schema_version'] in {4, 5, 15} and value['tokenizer_checks'] is not None:
                    raise ValueError('held_tokenizer_receipt_invalid')
                if request['schema_version'] in {5, 15} and value['selected_tokenizer_checks'] is not None:
                    raise ValueError('held_selected_tokenizer_receipt_invalid')
                if request['schema_version'] == 6 and value['model_layout_checks'] is not None:
                    raise ValueError('held_model_layout_receipt_invalid')
                if request['schema_version'] == 7 and value['tensor_loading_checks'] is not None:
                    raise ValueError('held_tensor_loading_receipt_invalid')
                if request['schema_version'] in {8, 14} and value['model_loading_checks'] is not None:
                    raise ValueError('held_model_loading_receipt_invalid')
                if request['schema_version'] == 9 and value['image_effects_checks'] is not None:
                    raise ValueError('held_image_effects_receipt_invalid')
                if request['schema_version'] == 10 and value['dataset_package_checks'] is not None:
                    raise ValueError('held_dataset_package_receipt_invalid')
                if request['schema_version'] in {11, 12, 13} and value['dataset_preparation_checks'] is not None:
                    raise ValueError('held_dataset_preparation_receipt_invalid')
                if request['schema_version'] in {12, 13} and value['model_pairing_checks'] is not None:
                    raise ValueError('held_model_pairing_receipt_invalid')
                if request['schema_version'] == 13 and value['conditioning_checks'] is not None:
                    raise ValueError('held_conditioning_receipt_invalid')
                if request["schema_version"] in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17} and value["native_observation_sha256"] != "":
                    raise ValueError("held_native_receipt_invalid")
                if value["status"] != "held" or value["checks"] is not None or value["reason"] not in {"training_loop_failed", "training_loop_boundary_failed", "training_loop_resources_held", "unet_step_failed", "unet_step_boundary_failed", "unet_step_gpu_capacity_held", "unet_step_cuda_precision_unavailable", "native_connectivity_failed", "parser_import_failed", "parser_contract_failed", "tokenizer_contract_failed", "tokenizer_boundary_failed", "model_layout_failed", "model_layout_boundary_failed", "tensor_loading_failed", "tensor_loading_boundary_failed", "model_loading_failed", "model_loading_boundary_failed", "image_effects_failed", "image_effects_boundary_failed", "dataset_package_failed", "dataset_package_boundary_failed", "dataset_preparation_failed", "dataset_preparation_boundary_failed", "model_pairing_failed", "model_pairing_boundary_failed", "conditioning_failed", "conditioning_boundary_failed", "probe_boundary_denied", "python_version_unsupported", "runtime_roots_mismatch", "native_observation_failed", "network_or_process_denied", "external_write_denied", "external_or_model_read_denied", "external_directory_denied"}:
                    raise ValueError("held_receipt_mismatch")
                report.update(reason=value["reason"], python_version=value["python_version"], parser_result_sha256=_sha(raw), denied_operation=value["denied_operation"], failure_site=value["failure_site"])
                return report
            expected_reason = ('generated_training_loop_only' if request['schema_version'] == 17 else
                               'generated_unet_lora_step_only' if request['schema_version'] == 16 else
                               'native_selected_tokenizers_and_synthetic_captions_only' if request['schema_version'] == 15 else
                               'generated_native_model_loading_only' if request['schema_version'] == 14 else
                               'generated_encoder_conditioning_only' if request['schema_version'] == 13 else
                               'generated_model_pairing_only' if request['schema_version'] in {12, 13} else
                               'selected_package_preparation_only' if request['schema_version'] in {11, 12, 13} else
                               'generated_dataset_package_only' if request['schema_version'] == 10 else
                               'generated_image_effects_only' if request['schema_version'] == 9 else
                               'generated_full_model_loading_only' if request['schema_version'] in {8, 14} else
                               'generated_local_tensor_loading_only' if request['schema_version'] == 7 else
                               'generated_meta_model_layout_only' if request['schema_version'] == 6 else
                               'selected_tokenizers_and_synthetic_captions_only' if request['schema_version'] in {5, 15} else
                               'synthetic_parser_and_tokenizers_only' if request['schema_version'] == 4 else 'synthetic_parser_only')
            if value["status"] != "parser_checked" or value["reason"] != expected_reason or value["denied_operation"] != "none" or value["failure_site"] or tuple(map(int, value["python_version"].split(".")[:2])) not in PYTHON_VERSIONS:
                raise ValueError("parser_success_mismatch")
            if value["software_roots_checked"] != (request["schema_version"] in {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}) or bool(plan.inventories) != value["software_roots_checked"]:
                raise ValueError("software_roots_unverified")
            checks = value["checks"]
            if type(checks) is not dict or set(checks) != {"cases", "parser_destinations", "defaults_sha256", "unknown_cli_rejected"} or type(checks["parser_destinations"]) is not int or not 20 <= checks["parser_destinations"] <= 1000 or checks["unknown_cli_rejected"] is not True or type(checks["defaults_sha256"]) is not str or not SHA.fullmatch(checks["defaults_sha256"]):
                raise ValueError("parser_checks_invalid")
            configs = synthetic_configurations()
            if type(checks["cases"]) is not list or len(checks["cases"]) != len(configs):
                raise ValueError("parser_cases_missing")
            for row, config in zip(checks["cases"], configs):
                if type(row) is not dict or set(row) != {"requested_sha256", "effective_sha256"} or row["requested_sha256"] != _sha(_bytes(config)) or type(row["effective_sha256"]) is not str or not SHA.fullmatch(row["effective_sha256"]):
                    raise ValueError("parser_case_invalid")
            bound_checks = checks
            if request['schema_version'] in {4, 5, 15}:
                tokenizers = validate_tokenizer_report(value['tokenizer_checks'])
                bound_checks = {'parser': checks, 'tokenizers': tokenizers}
            if request['schema_version'] in {5, 15}:
                selected_checks = selected_tokens.validate_report(value['selected_tokenizer_checks'], request['selected_tokenizers']['binding'])
                bound_checks['selected_tokenizers'] = selected_checks
            if request['schema_version'] == 6:
                catalog_raw = _read(plan.destination / 'work' / model_layout.CATALOG_FILE, limit=model_layout.MAX_CATALOG)
                catalog = model_layout.validate_catalog(_json(catalog_raw))
                if catalog_raw != model_layout.canonical(catalog):
                    raise ValueError('noncanonical_model_layout')
                model_checks = model_layout.validate_report(value['model_layout_checks'], catalog)
                bound_checks = {'parser': checks, 'model_layout': model_checks}
            if request['schema_version'] == 7:
                tensor_checks = tensor_loading.validate_report(value['tensor_loading_checks'])
                bound_checks = {'parser': checks, 'tensor_loading': tensor_checks}
            if request['schema_version'] in {8, 14}:
                full_model_checks = model_loading.validate_report(value['model_loading_checks'])
                bound_checks = {'parser': checks, 'model_loading': full_model_checks}
            if request['schema_version'] == 9:
                image_checks = image_effects.validate_report(value['image_effects_checks'])
                bound_checks = {'parser': checks, 'image_effects': image_checks}
            if request['schema_version'] == 10:
                package_checks = dataset_package.validate_report(value['dataset_package_checks'])
                bound_checks = {'parser': checks, 'dataset_package': package_checks}
            if request['schema_version'] in {11, 12, 13}:
                preparation_checks = dataset_preparation.validate_report(value['dataset_preparation_checks'], request['dataset_preparation'])
                bound_checks = {'parser': checks, 'dataset_preparation': preparation_checks}
            if request['schema_version'] in {12, 13}:
                pairing_checks = model_pairing_contract.validate_report(value['model_pairing_checks'], request['dataset_preparation'], request['model_pairing'])
                bound_checks = {'parser': checks, 'dataset_preparation': preparation_checks, 'model_pairing': pairing_checks}
            if request['schema_version'] == 13:
                conditioning_checks = conditioning_contract.validate_report(value['conditioning_checks'], request['dataset_preparation'], request['model_pairing'])
                bound_checks['conditioning'] = conditioning_checks
            if request['schema_version'] == 16:
                unet_checks = unet_step.validate_report(value['unet_step_checks'])
                verify_unet_outputs(unet_checks, plan.destination / 'work')
                bound_checks = {'parser': checks, 'unet_step': unet_checks}
            if request['schema_version'] == 17:
                loop_checks = training_loop.validate_report(value['training_loop_checks'])
                verify_loop_outputs(loop_checks, plan.destination / 'work')
                bound_checks = {'parser': checks, 'training_loop': loop_checks}
            if request['schema_version'] in {14, 15, 16, 17}:
                local_checks = native_connectivity_checks(request['schema_version'])
                # Imports may handle a denied optional file read. The probe
                # clears those denials before the experiment and refuses success
                # on any new experiment-phase denial. Retain the import evidence;
                # a network/process denial cannot belong to the native mode.
                file_denials = {'external_write_denied', 'external_or_model_read_denied', 'external_directory_denied'}
                if _bytes(value['local_processing_checks']) != _bytes(local_checks) or any(item not in file_denials for item in operations):
                    raise ValueError('native_connectivity_invalid')
                bound_checks['local_processing'] = local_checks
            if request["schema_version"] in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
                observed_hash = value["native_observation_sha256"]
                if type(observed_hash) is not str or not SHA.fullmatch(observed_hash) or rows[-1]["details"].get("native_observation_sha256") != observed_hash:
                    raise ValueError("native_observation_unbound")
                report["native_module_observation"] = verify_observation(plan, request, bound_checks, observed_hash)
                report["native_observation_sha256"] = observed_hash
        except (OSError, ValueError, TypeError, KeyError, TrainingProcessError):
            report["reason"] = "parser_evidence_invalid"
            return report
        report.update(status="parser_checked", reason=expected_reason, checks=checks,
                      parser_result_sha256=_sha(raw), python_version=value["python_version"])
        if request['schema_version'] in {4, 5, 15}:
            report.update(tokenizer_checks=tokenizers, synthetic_tokenizers_verified=True)
        if request['schema_version'] in {5, 15}:
            report.update(selected_tokenizer_checks=selected_checks, selected_tokenizer_behavior_verified=True)
        if request['schema_version'] == 6:
            report.update(model_layout_checks=model_checks, synthetic_model_layout_verified=True)
        if request['schema_version'] == 7:
            report.update(tensor_loading_checks=tensor_checks, synthetic_tensor_loading_verified=True)
        if request['schema_version'] in {8, 14}:
            report.update(model_loading_checks=full_model_checks, synthetic_model_loading_verified=True)
        if request['schema_version'] == 9:
            report.update(image_effects_checks=image_checks, synthetic_image_effects_verified=True)
        if request['schema_version'] == 10:
            report.update(dataset_package_checks=package_checks, synthetic_dataset_package_verified=True)
        if request['schema_version'] in {11, 12, 13}:
            report.update(dataset_preparation_checks=preparation_checks, package_preparation_verified=True)
        if request['schema_version'] in {12, 13}:
            report.update(model_pairing_checks=pairing_checks, generated_model_pairing_verified=True)
        if request['schema_version'] == 13:
            report.update(conditioning_checks=conditioning_checks, generated_encoder_conditioning_verified=True)
        if request['schema_version'] == 16:
            report['unet_step_checks'] = unet_checks
        if request['schema_version'] == 17:
            report['training_loop_checks'] = loop_checks
        if request['schema_version'] in {14, 15, 16, 17}:
            report[flag] = True
            report['local_processing_checks'] = local_checks
        return report
