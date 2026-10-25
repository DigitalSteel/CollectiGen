"""Portable software content identity, separate from local acquisition consent.

Catalogs contain relative source paths, bytes, hashes and attribution. Local
destinations, filesystem identities and health receipts never enter this identity.
A catalog is metadata only and cannot authorize acquisition or execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .training_file_inventory import MAX_BYTES, MAX_FILES, MAX_MANIFEST_BYTES, canonical
from .trainer_environment import (TrainerEnvironmentError, _DATA_DIRECTORIES, _DATA_SUFFIXES,
    _decode, _digest, _relative, _source, _text)
from .trainer_runtime_acquisition import MAX_ARTIFACTS, RuntimeAcquisitionPlan, validate_artifact_set


HEADLESS_POLICY = 'sdxl-windows-headless-v2'
KIND = 'trainer-runtime-content-catalog'


@dataclass(frozen=True)
class RuntimeSoftwareCatalog:
    _canonical: bytes

    @classmethod
    def from_dict(cls, value):
        raw = canonical(value)
        if len(raw) > MAX_MANIFEST_BYTES: raise TrainerEnvironmentError('Runtime content catalog exceeds its bound.')
        value = _decode(raw)
        if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'headless_policy',
                'python', 'wheels', 'direct_packages', 'lock_sha256', 'source'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['kind'] != KIND or value['headless_policy'] != HEADLESS_POLICY):
            raise TrainerEnvironmentError('Unsupported runtime content catalog.')
        validate_artifact_set(value['python'], value['wheels']); _digest(value['lock_sha256'])
        direct = value['direct_packages']
        if (type(direct) is not list or not 0 < len(direct) <= MAX_ARTIFACTS
                or any(type(item) is not str for item in direct)
                or not set(direct) <= {row['name'] for row in value['wheels']}):
            raise TrainerEnvironmentError('Catalog direct dependencies are unavailable.')
        source = value['source']
        if (type(source) is not dict or set(source) != {'name', 'revision', 'url', 'license', 'files'}
                or source['name'] != 'sd-scripts' or type(source['revision']) is not str
                or re.fullmatch('[0-9a-f]{40}', source['revision']) is None):
            raise TrainerEnvironmentError('Catalog trainer revision is invalid.')
        _source(source['url'], 'trainer source')
        license_info = source['license']
        if type(license_info) is not dict or set(license_info) != {'id', 'sha256', 'url'}:
            raise TrainerEnvironmentError('Catalog source license is unavailable.')
        _text(license_info['id'], 'source license'); _digest(license_info['sha256']); _source(license_info['url'], 'source license')
        files = source['files']
        if type(files) is not list or not 0 < len(files) <= MAX_FILES:
            raise TrainerEnvironmentError('Catalog source inventory exceeds its bound.')
        names, parents, total = set(), set(), 0
        for row in files:
            if type(row) is not dict or set(row) != {'path', 'size_bytes', 'sha256'}:
                raise TrainerEnvironmentError('Catalog source file has invalid fields.')
            path = _relative(row['path']); _digest(row['sha256'])
            if (path.suffix.casefold() in _DATA_SUFFIXES or path.suffix.casefold() in {'.pth', '.pkl', '.pickle'}
                    or row['path'].casefold().endswith('.pth.tar') or path.parts[0].casefold() in _DATA_DIRECTORIES):
                raise TrainerEnvironmentError('A trainer source catalog cannot contain model or dataset files.')
            folded = row['path'].casefold()
            ancestor_names = {parent.as_posix().casefold() for parent in path.parents if parent.as_posix() != '.'}
            if folded in names or folded in parents or ancestor_names & names:
                raise TrainerEnvironmentError('Catalog source paths collide.')
            if type(row['size_bytes']) is not int or row['size_bytes'] < 0:
                raise TrainerEnvironmentError('Catalog source size is invalid.')
            names.add(folded); parents.update(ancestor_names); total += row['size_bytes']
            if total > MAX_BYTES: raise TrainerEnvironmentError('Catalog source bytes exceed their bound.')
        if (not any(row['path'] == 'LICENSE.md' and row['sha256'] == license_info['sha256'] for row in files)
                or not any(row['path'] == 'sdxl_train_network.py' for row in files)):
            raise TrainerEnvironmentError('Catalog source entry point or license identity is missing.')
        value['direct_packages'] = sorted(set(direct))
        value['wheels'] = sorted(value['wheels'], key=lambda row: row['filename'])
        source['files'] = sorted(files, key=lambda row: row['path'])
        return cls(canonical(value))

    @classmethod
    def from_plan(cls, plan, source_files):
        """Convert already verified development metadata; never read its roots."""
        value = RuntimeAcquisitionPlan.from_dict(plan.payload()).payload()
        return cls.from_dict({'schema_version': 1, 'kind': KIND, 'headless_policy': HEADLESS_POLICY,
            'python': value['python'], 'wheels': value['wheels'], 'direct_packages': value['direct_packages'],
            'lock_sha256': value['lock_sha256'], 'source': {'name': 'sd-scripts', 'revision': value['source_revision'],
                'url': value['source_origin'], 'license': {name: value['source_license'][name] for name in ('id', 'sha256', 'url')},
                'files': source_files}})

    @property
    def fingerprint(self): return hashlib.sha256(self._canonical).hexdigest()

    def payload(self): return _decode(self._canonical)

    @property
    def source_files(self): return tuple(self.payload()['source']['files'])

    @property
    def source_digest(self):
        return hashlib.sha256(canonical({'schema_version': 1, 'kind': 'trainer-source-content',
                                         'files': list(self.source_files)})).hexdigest()

    def validate_plan(self, plan):
        value = plan.payload()
        if (self.from_plan(plan, list(self.source_files)).fingerprint != self.fingerprint
                or value.get('catalog_sha256', self.fingerprint) != self.fingerprint):
            raise TrainerEnvironmentError('Runtime catalog changed after the local acquisition review.')

    def installation_plan(self, *, destination, candidate_workspace, source_root, storage_budget_bytes, storage_scope):
        """Build a local review whose fingerprint binds every chosen location."""
        value = self.payload(); source = value['source']; source_root = Path(source_root).absolute()
        return RuntimeAcquisitionPlan.from_dict({'schema_version': 1, 'status': 'review_required',
            'scope': 'Windows x64 headless SDXL LoRA runtime candidate',
            'python': value['python'], 'wheels': value['wheels'], 'direct_packages': value['direct_packages'],
            'lock_sha256': value['lock_sha256'], 'source_revision': source['revision'], 'source_origin': source['url'],
            'source_acquisition': 'Reuse only the explicitly selected local source matching the pinned relative content catalog.',
            'download_bytes': value['python']['bytes'] + sum(row['bytes'] for row in value['wheels']),
            'destination': str(Path(destination).absolute()), 'candidate_workspace': str(Path(candidate_workspace).absolute()),
            'storage_budget_bytes': storage_budget_bytes, 'storage_scope': storage_scope,
            'qualification': ['exact software and source verification', 'non-mutating health',
                              'native training and recovery', 'offline pack relocation'],
            'execution_ready': False, 'existing_model_folders_accessed': False,
            'source_license': {**source['license'], 'path': str(source_root / 'LICENSE.md')},
            'source_inventory': None, 'catalog_sha256': self.fingerprint, 'source_root': str(source_root),
            'assembly_policy': {'install': 'Pinned standalone Python and wheels; no resolver, source build or application activation.',
                'candidate': 'Health, native execution, recovery and offline relocation remain separate gates.',
                'downloads': 'Only the exact catalog artifacts; no model or dataset acquisition.',
                'publication': 'No upload or publication is authorized.'}})
