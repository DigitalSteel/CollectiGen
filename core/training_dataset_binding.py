"""Compose full package verification, admission and contained native preparation."""
from __future__ import annotations

from dataclasses import asdict
import io
import os
from pathlib import Path
import re
from uuid import uuid4
import zipfile

from . import training_dataset_preparation as selected
from .training_preflight import ContainedRuntimeQualificationService, PreflightReply
from .training_process import ProcessInput, TrainingProcessError, _bytes, _sha, _write_new
from .training_source_io import _assert_unredirected, read_stable_training_source

MAX_VERIFIER_SOURCE_BYTES = 32 * 1024 * 1024

WORKER = Path(__file__).resolve().parents[1] / 'tools/training_dataset_profile_worker.py'
# Direct shared verification/manifest/caption/transform authorities. These are
# controller-owned process inputs; no library, settings or registry is loaded.
PACKAGE_CODE = tuple(Path(__file__).with_name(name) for name in (
    'training_dataset_binding.py', 'training_dataset_preparation.py', 'training_package.py',
    'training_candidate_selection.py', 'training_plan_drafts.py', 'training_configuration.py',
    'training_image_transform.py', 'training_dataset_analysis.py', 'training_readiness.py',
    'external_write_transaction.py', 'external_write_policy.py', 'export_profile.py',
    'export_profile_store.py', 'federated_dataset.py'))


def verification_bundle():
    """Snapshot project Python sources; imported verifier dependencies are bound.

    No data/configuration/cache files enter this archive. The worker supplies an
    empty package object so the application initializer never runs.
    """
    folder = Path(__file__).parent
    entries = [(path, '_collectigen_package_code/' + path.relative_to(folder).as_posix())
               for path in sorted(folder.rglob('*.py'))]
    entries.append((folder.parent / 'version.py', 'version.py'))
    stream = io.BytesIO()
    remaining = MAX_VERIFIER_SOURCE_BYTES
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for path, name in entries:
            if remaining == 0:
                raise ValueError('dataset_verifier_code_scope_exceeded')
            raw, _ = read_stable_training_source(path, max_bytes=remaining)
            remaining -= len(raw)
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), raw)
    return stream.getvalue()


def write_verification_bundle(path):
    raw = verification_bundle()
    _assert_unredirected(path)
    with path.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    captured = ProcessInput.capture(path)
    if captured.sha256 != selected.sha(raw) or captured.state[2] != len(raw):
        raise ValueError('dataset_verifier_code_changed')
    return captured.sha256


def verify_package(selection, *, should_cancel=None):
    """Blocking helper primitive; never call on the UI/controller thread."""
    from .training_package import TrainingPackageService, TrainingPackageVerificationLimits
    if should_cancel and should_cancel():
        raise InterruptedError('Dataset package verification was cancelled.')
    selected.validate_selection(selection)
    root = Path(selection['root'])
    _assert_unredirected(root / selected.generated.MANIFEST)
    expected = selected.read_manifest(root, selected.binding(selection))
    if sum(row['size'] for row in expected['artifacts']) > selected.MAX_PACKAGE_BYTES:
        raise ValueError('dataset_scope_exceeded')
    from PIL import Image
    decoded_pixels = 0
    for artifact in expected['artifacts']:
        if artifact['kind'] != 'image':
            continue
        if should_cancel and should_cancel():
            raise InterruptedError('Dataset package verification was cancelled.')
        path = root / artifact['path']
        _assert_unredirected(path)
        if not path.is_relative_to(root):
            raise ValueError('dataset_path_invalid')
        with Image.open(path) as image:
            width, height = image.size
            decoded_pixels += width * height
            if image.size != tuple(artifact['transform']['output_dimensions']) or width * height > 4 * 1024 * 1024 or decoded_pixels > selected.MAX_PIXEL_SAMPLES:
                raise ValueError('dataset_scope_exceeded')
    limits = TrainingPackageVerificationLimits(
        manifest_bytes=selected.MAX_MANIFEST,
        artifact_bytes=selected.MAX_PACKAGE_BYTES,
        total_bytes=selected.MAX_MANIFEST + selected.MAX_PACKAGE_BYTES,
        image_pixels=4 * 1024 * 1024,
    )
    verified = TrainingPackageService().verify(root, should_cancel=should_cancel, limits=limits)
    if not verified.valid or verified.manifest != expected or verified.package_fingerprint != selection['package_fingerprint']:
        raise ValueError('package_unavailable_or_changed')
    return verified


def inspect_package(selection):
    package = verify_package(selection)
    summary = selected.assess_manifest(package.manifest)
    return selected.profile_report(selected.binding(selection), summary,
        'policy_held' if summary['holds'] else 'admitted')


def prepare_replica(selection, expected_summary, destination, *, should_cancel=None):
    """Copy a verified package to a fresh helper-owned immutable input root."""
    from .training_package import TrainingPackageService
    package = verify_package(selection, should_cancel=should_cancel)
    summary = selected.assess_manifest(package.manifest)
    if summary['holds'] or selected.canonical(summary) != selected.canonical(expected_summary):
        raise ValueError('dataset_admission_changed')
    if sum(row['size'] for row in package.manifest['artifacts']) > selected.MAX_PACKAGE_BYTES:
        raise ValueError('dataset_scope_exceeded')
    destination = Path(destination)
    if destination.is_relative_to(package.destination):
        raise ValueError('dataset_workspace_overlap')
    destination.mkdir()
    artifacts = [(selected.generated.MANIFEST, None)]
    artifacts.extend((row['path'], row['size']) for row in package.manifest['artifacts'])
    for name, expected_size in artifacts:
        if should_cancel and should_cancel():
            raise InterruptedError('Dataset replica preparation was cancelled.')
        source, target = package.destination / name, destination / name
        _assert_unredirected(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as reader, target.open('xb') as writer:
            size = os.fstat(reader.fileno()).st_size
            if expected_size is None:
                if size > selected.MAX_MANIFEST:
                    raise ValueError('dataset_scope_exceeded')
                expected_size = size
            if size != expected_size:
                raise ValueError('dataset_replica_changed')
            remaining = expected_size
            while remaining:
                if should_cancel and should_cancel():
                    raise InterruptedError('Dataset replica preparation was cancelled.')
                block = reader.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError('dataset_replica_changed')
                writer.write(block)
                remaining -= len(block)
            if reader.read(1):
                raise ValueError('dataset_replica_changed')
    replica = TrainingPackageService().verify(destination, should_cancel=should_cancel)
    if not replica.valid or replica.manifest != package.manifest or verify_package(selection, should_cancel=should_cancel).manifest != package.manifest:
        raise ValueError('dataset_replica_changed')
    return destination


class DatasetReply(PreflightReply):
    def decode(self, value, plan):
        reasons = {'admitted', 'policy_held', 'package_unavailable_or_changed'}
        if type(value) is not dict or value.get('reason') not in reasons:
            raise ValueError('dataset_profile_invalid')
        expected_summary = None if value['reason'] == 'package_unavailable_or_changed' else self.request['summary']
        expected = selected.profile_report(selected.binding(self.request['selection']), expected_summary, value['reason'])
        if _bytes(value) != _bytes(expected) or (value['reason'] == 'admitted' and expected_summary['holds']) or (value['reason'] == 'policy_held' and not expected_summary['holds']):
            raise ValueError('dataset_profile_changed')
        return (value,)


class ContainedDatasetPreparationService(ContainedRuntimeQualificationService):
    def check(self, selection, summary, root, jobs, *, should_cancel=None, progress=None):
        selected.validate_selection(selection)
        root = Path(os.path.abspath(root))
        if root.is_relative_to(Path(selection['root'])):
            raise TrainingProcessError('Keep preparation work outside the selected package.')
        request = {'schema_version': 1, 'nonce': uuid4().hex, 'operation': 'dataset_admission',
            'selection': selection, 'summary': summary}
        report, receipt = self._run(request, root, jobs, worker=WORKER, extra_code=PACKAGE_CODE,
            reply_type=DatasetReply, should_cancel=should_cancel, progress=progress, timeout_seconds=300)
        return {**report, 'preflight_receipt_sha256': _sha(_bytes(receipt))}

    def prepare_package(self, package, recipe, source, environment, python_base, root, jobs,
                        *, profile='kohya-uv-45088f04', should_cancel=None, progress=None, generated_model_pairing=False, generated_conditioning=False):
        """Explicit selected-package processing only; no training-launch consumer.

        The caller owns package/privacy authority and runs this off the UI thread.
        The cached result supplies an expected identity, never current evidence.
        """
        recipe.validate()
        if type(generated_model_pairing) is not bool or type(generated_conditioning) is not bool:
            raise ValueError('Choose generated model pairing explicitly.')
        if not package.valid or type(package.manifest) is not dict:
            raise ValueError('Start from a verified training package.')
        selection = selected.validate_selection({'root': str(Path(os.path.abspath(package.destination))),
            'package_fingerprint': package.package_fingerprint,
            'manifest_sha256': selected.digest(package.manifest), 'recipe': asdict(recipe)})
        summary = selected.assess_manifest(package.manifest)
        if generated_conditioning:
            generated_model_pairing = True
        pairing = None
        if generated_model_pairing:
            from .training_configuration import TrainingConfiguration
            from . import training_model_pairing_contract as pairing_contract
            config = TrainingConfiguration.from_dict(package.manifest['training_configuration'])
            from .training_model_loading_contract import FIXTURE_SHA256
            if config.base_model_id != 'sha256:' + FIXTURE_SHA256['base']:
                raise ValueError('Generated pairing requires the exact generated base identity.')
            pairing = {'caption_limit': config.max_caption_tokens}
            pairing_contract.validate_manifest_scope(package.manifest,
                {'binding': selected.binding(selection), 'summary': summary}, pairing)
        before = self.check(selection, summary, root, jobs, should_cancel=should_cancel, progress=progress)
        report = {'schema_version': 1, 'kind': 'sdxl_dataset_preparation_qualification',
            'status': 'held', 'reason': before['reason'], 'binding': selected.binding(selection),
            'package_checks': [before], 'runtime_check': None, 'package_preparation_verified': False,
            'selected_dataset_verified': False, 'model_compatibility_verified': False,
            'offline_execution_verified': False, 'runtime_code_verified': False, 'execution_ready': False}
        if pairing is not None:
            report.update(generated_model_pairing_verified=False)
        if generated_conditioning:
            report.update(generated_encoder_conditioning_verified=False)
        if before['status'] != 'admitted':
            return report, None
        memory = selected.SelectionMemory(selection)
        try:
            runtime_report, destination = self.qualify(source, environment, root, jobs,
                profile=profile, probe_parser=True, python_base=python_base,
                selected_dataset={'memory': memory.descriptor, 'binding': selected.binding(selection), 'summary': summary},
                model_pairing=pairing, conditioning=generated_conditioning,
                should_cancel=should_cancel, progress=progress)
        finally:
            memory.close()
        report['runtime_check'] = runtime_report
        after = self.check(selection, summary, root, jobs, should_cancel=should_cancel, progress=progress)
        report['package_checks'].append(after)
        if after['status'] != 'admitted':
            report['reason'] = 'package_changed_after_preparation'
        elif runtime_report.get('status') != 'parser_checked' or runtime_report.get('package_preparation_verified') is not True:
            report['reason'] = 'dataset_runtime_held'
        else:
            selected.validate_report(runtime_report['dataset_preparation_checks'], {'binding': selected.binding(selection), 'summary': summary})
            report.update(status='package_prepared', reason='one_epoch_pixel_preserving_preparation_only',
                package_preparation_verified=True)
        if pairing is not None and report['package_preparation_verified']:
            try:
                if runtime_report.get('generated_model_pairing_verified') is not True:
                    raise ValueError('Generated model pairing is held.')
                pairing_contract.validate_report(runtime_report['model_pairing_checks'],
                    {'binding': selected.binding(selection), 'summary': summary}, pairing)
            except (KeyError, ValueError, TypeError):
                report.update(status='held', reason='model_pairing_held', package_preparation_verified=False)
            else:
                report.update(status='generated_pairing_checked', reason='generated_encoder_input_wiring_only',
                    generated_model_pairing_verified=True)
        if generated_conditioning and report.get('generated_model_pairing_verified'):
            from . import training_conditioning_contract as conditioning_contract
            try:
                if runtime_report.get('generated_encoder_conditioning_verified') is not True:
                    raise ValueError('Generated conditioning is held.')
                conditioning_contract.validate_report(runtime_report['conditioning_checks'],
                    {'binding': selected.binding(selection), 'summary': summary}, pairing)
            except (KeyError, ValueError, TypeError):
                report.update(status='held', reason='conditioning_held', package_preparation_verified=False, generated_model_pairing_verified=False)
            else:
                report.update(status='generated_conditioning_checked', reason='generated_encoder_conditioning_only', generated_encoder_conditioning_verified=True)
        if destination is not None:
            _write_new(destination / 'dataset-preparation-qualification.json', _bytes(report))
        return report, destination


class TrainingPreparationRetentionError(TrainingProcessError):
    """The prepared workspace exists, but its history pointer could not be saved."""

    def __init__(self, workspace):
        super().__init__("The dataset was prepared, but its receipt could not be saved to history.")
        self.workspace = workspace


class ReviewedDatasetVerifier:
    """Bind Manager review to an explicitly reviewed worker and owned work directory."""

    def __init__(self, inventory, python_relative, work_root):
        from .training_process import ProcessInventory

        if type(inventory) is not ProcessInventory or not Path(work_root).is_absolute():
            raise ValueError("An exact reviewed worker and absolute Training work root are required.")
        self.inventory = inventory
        self.python_relative = python_relative
        self.work_root = Path(work_root)

    @classmethod
    def from_verified_file_rows(cls, runtime, rows, manifest, work_root, *, should_cancel=None):
        """Pin an installed copy using file rows already authenticated by its owner."""
        from .training_file_inventory import SoftwareInventoryService
        from .training_process import ProcessInventory

        runtime = Path(runtime)
        manifest = Path(manifest)
        work_root = Path(work_root)
        if (not runtime.is_absolute() or not work_root.is_absolute()
                or not manifest.is_absolute() or manifest.parent != work_root):
            raise TrainingProcessError("Use an absolute worker root and a separate owned inventory directory.")
        service = SoftwareInventoryService()
        inventory = service.capture(runtime, should_cancel=should_cancel)
        actual = {name: {"directory": True} for name, _ in inventory.directories if name != "."}
        actual.update({name: {"bytes": state[2], "sha256": digest}
                       for name, state, digest in inventory.files})
        if actual != rows or "python.exe" not in actual or "sha256" not in actual["python.exe"]:
            raise TrainingProcessError("The installed Training worker differs from its verified package files.")
        service.save(inventory, manifest)
        pin = ProcessInventory(inventory.root, str(manifest), inventory.fingerprint,
                               len(inventory.files), inventory.byte_count)
        return cls(pin, "python.exe", work_root)

    def _selected_package(self, package):
        from .training_adapter_preview import SdxlLoraRecipe

        package_root = Path(package.destination).resolve(strict=True)
        root = self.work_root.resolve(strict=True)
        if any(root.is_relative_to(path) for path in
               (package_root, Path(self.inventory.root).resolve(strict=True))):
            raise TrainingProcessError("Use a separate owned directory for dataset verification.")
        selection = {
            "root": str(package_root),
            "package_fingerprint": package.package_fingerprint,
            "manifest_sha256": selected.digest(package.manifest),
            "recipe": asdict(SdxlLoraRecipe()),
        }
        return root, selection

    def prepare(self, package, *, source, environment, python_base, jobs,
                should_cancel=None, progress=None):
        """Prepare a selected SDXL dataset using the same reviewed worker inventory."""
        from tempfile import mkdtemp

        paths = tuple(Path(path) for path in (source, environment, python_base))
        if any(not path.is_absolute() or ".." in path.parts for path in paths):
            raise TrainingProcessError("Select explicit absolute trainer source, environment and base Python directories.")
        root, selection = self._selected_package(package)
        if any(root.is_relative_to(path) for path in paths):
            raise TrainingProcessError("Use a separate owned directory for Training preparation.")
        worker = ContainedRuntimeQualificationService.from_reviewed_worker_inventory(
            self.inventory, self.python_relative, should_cancel=should_cancel,
        )
        work = Path(mkdtemp(prefix="dataset-preparation-", dir=root))
        memory = selected.SelectionMemory(selection)
        try:
            result = worker.preflight(paths[0], paths[1], work, jobs, prepare=True,
                python_base=paths[2], selected_dataset={"memory": memory.descriptor,
                    "binding": selected.binding(selection), "summary": selected.assess_manifest(package.manifest)},
                should_cancel=should_cancel, progress=progress)
            if result[1] is not None:
                from .training_process import TrainingRunJournal
                from .training_run_index import ScopedPreparationHistory

                receipt = result[3]
                nonce = receipt["nonce"]
                if type(nonce) is not str or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
                    raise TrainingProcessError("The completed preparation receipt has an invalid identity.")
                directory = work / ("preflight-" + nonce)
                try:
                    _, tip = TrainingRunJournal(directory).read()
                    ScopedPreparationHistory(root).retain(
                        directory, receipt_sha256=_sha(_bytes(receipt)), journal_tip=tip)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise TrainingPreparationRetentionError(result[1].root) from exc
            return result
        finally:
            memory.close()

    def __call__(self, package, *, should_cancel):
        from tempfile import mkdtemp

        from .background_jobs import BackgroundJobManager

        root, selection = self._selected_package(package)
        worker = ContainedDatasetPreparationService.from_reviewed_worker_inventory(
            self.inventory, self.python_relative, should_cancel=should_cancel,
        )
        work = Path(mkdtemp(prefix="dataset-review-", dir=root))
        jobs = BackgroundJobManager()
        try:
            return worker.check(selection, selected.assess_manifest(package.manifest), work, jobs,
                                should_cancel=should_cancel)
        finally:
            jobs.shutdown(wait=True)
