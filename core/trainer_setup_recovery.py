"""Bounded Setup recovery records for exact reviewed operations and native owners."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re

from .trainer_environment import TrainerEnvironmentService, _read_json, _write_json, _decode
from .training_file_inventory import _check_path
from .training_source_io import _assert_unredirected
from .training_process import (TrainingProcessError, TrainingProcessService, TrainingRunJournal,
    PENDING_NAME, _bytes, _directory, _lease)
from .training_run_recovery import _plan

MAX_EVENTS = 256
MAX_EVENT_BYTES = 256 * 1024
MAX_JOURNAL_BYTES = 8 * 1024**2
SHA = re.compile(r'[0-9a-f]{64}')
GENERATION = re.compile(r'[0-9a-f]{32}')


class TrainerSetupRecoveryRequired(RuntimeError):
    pass


class TrainerSetupRecovery:
    """One append-only receipt chain per confirmed Setup operation.

    Construction performs no IO. Records carry exact reviewed references, never
    imply that a native process stopped, and never activate or delete software.
    The shared queue executor must also hold its operation execution lease.
    """
    _locked = TrainerEnvironmentService._locked

    def __init__(self, root, review_sha256):
        if type(review_sha256) is not str or not SHA.fullmatch(review_sha256):
            raise ValueError('A reviewed Setup fingerprint is required.')
        self.root = Path(root).absolute()
        self.review_sha256 = review_sha256

    def _read(self):
        _assert_unredirected(self.root)
        if not self.root.exists():
            return [], ''
        _check_path(self.root, directory=True)
        def names():
            found = []
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if entry.name == '.operation.lock':
                        continue
                    if len(found) >= MAX_EVENTS or not re.fullmatch(r'[0-9]{3}\.json', entry.name):
                        raise TrainerSetupRecoveryRequired('Setup recovery contains an unexpected record.')
                    found.append(entry.name)
            return sorted(found)
        selected = names()
        if selected != [f'{index:03}.json' for index in range(len(selected))]:
            raise TrainerSetupRecoveryRequired('Setup recovery records are incomplete.')
        rows, tip, size, identities = [], '', 0, []
        for index, name in enumerate(selected):
            path = self.root / name
            before = _check_path(path)
            value = _read_json(path, limit=MAX_EVENT_BYTES)
            if _check_path(path) != before:
                raise TrainerSetupRecoveryRequired('Setup recovery changed while reading.')
            identities.append((path,before))
            raw = _bytes(value); size += len(raw)
            if (type(value) is not dict or set(value) != {'schema_version','kind','sequence',
                    'previous','review_sha256','event','details'}
                    or type(value['schema_version']) is not int or value['schema_version'] != 1
                    or value['kind'] != 'trainer_setup_recovery_event'
                    or type(value['sequence']) is not int or value['sequence'] != index
                    or value['previous'] != tip or value['review_sha256'] != self.review_sha256
                    or type(value['event']) is not str or not re.fullmatch(r'[a-z_]{1,64}',value['event'])
                    or type(value['details']) is not dict or size > MAX_JOURNAL_BYTES):
                raise TrainerSetupRecoveryRequired('Setup recovery evidence changed or exceeds its bound.')
            rows.append(value); tip = hashlib.sha256(raw).hexdigest()
        if names() != selected or any(_check_path(path) != identity for path,identity in identities):
            raise TrainerSetupRecoveryRequired('Setup recovery changed while reading.')
        return rows, tip

    def snapshot(self):
        with self._locked():
            return self._read()

    def append(self, event, details):
        if type(event) is not str or not re.fullmatch(r'[a-z_]{1,64}',event) or type(details) is not dict:
            raise ValueError('Invalid Setup recovery event.')
        with self._locked():
            rows, tip = self._read()
            value = {'schema_version':1,'kind':'trainer_setup_recovery_event',
                'sequence':len(rows),'previous':tip,'review_sha256':self.review_sha256,
                'event':event,'details':details}
            raw = _bytes(value)
            if (len(rows) >= MAX_EVENTS or len(raw) > MAX_EVENT_BYTES
                    or sum(len(_bytes(row)) for row in rows)+len(raw) > MAX_JOURNAL_BYTES):
                raise TrainerSetupRecoveryRequired('Setup recovery storage is full; preserve this operation.')
            path = self.root / f'{len(rows):03}.json'
            _write_json(path,_decode(raw))
            if _bytes(_read_json(path,limit=MAX_EVENT_BYTES)) != raw:
                raise TrainerSetupRecoveryRequired('Setup recovery failed read-back verification.')
            return hashlib.sha256(raw).hexdigest()

    def retain_acquisition(self, attempt, fingerprint):
        if type(fingerprint) is not str or not SHA.fullmatch(fingerprint):
            raise ValueError('Invalid acquisition fingerprint.')
        return self.append('acquisition_started',{'attempt':str(Path(attempt).absolute()),
            'plan_fingerprint':fingerprint})

    def retain_stage(self, managed_root, stage, profile_sha256):
        managed_root,stage = Path(managed_root).absolute(),Path(stage).absolute()
        if (stage.parent != managed_root/'staging' or not GENERATION.fullmatch(stage.name)
                or type(profile_sha256) is not str or not SHA.fullmatch(profile_sha256)):
            raise ValueError('The retained stage is outside the selected managed environment.')
        return self.append('stage_started',{'generation_id':stage.name,'stage':str(stage),
            'final':str(managed_root/'generations'/stage.name),'profile_sha256':profile_sha256})

    def retain_health(self, plan):
        plan.validate()
        return self.append('health_started',{'plan':asdict(plan),'fingerprint':plan.fingerprint})

    def reconcile_health(self, work_root, *, process=None):
        """Prove exact prior owners stopped before an explicitly retried operation.

        A missing journal is accepted only when the reserved destination does not
        exist, its root identity is unchanged, no pending owner exists, and the
        named Windows Job is absent under the same root lease used for launch.
        Partial/torn or mismatched evidence is retained for explicit recovery.
        """
        selected_root = Path(work_root).absolute()
        process = process if process is not None else TrainingProcessService()
        rows, _ = self.snapshot()
        plans = []
        for row in rows:
            if row['event'] != 'health_started':
                continue
            details = row['details']
            if set(details) != {'plan','fingerprint'}:
                raise TrainerSetupRecoveryRequired('The retained health plan is invalid.')
            plan = _plan(details['plan'])
            if Path(plan.root) != selected_root or plan.fingerprint != details['fingerprint']:
                raise TrainerSetupRecoveryRequired('The retained health owner differs from Setup.')
            plans.append(plan)
        results = []
        for plan in plans:
            if os.path.lexists(plan.destination):
                prior, tip = TrainingRunJournal(plan.destination).read()
                if (not prior or _bytes(prior[0]['details'].get('plan'))
                        != _bytes(TrainingProcessService._receipt(plan))):
                    raise TrainerSetupRecoveryRequired('The retained health process journal is incomplete or changed.')
                result = process.recover(plan.destination,tip)
                if result.tree_stopped is not True:
                    raise TrainerSetupRecoveryRequired('Trainer health process termination remains unverified.')
                state = {'fingerprint':plan.fingerprint,'status':result.status,
                         'tree_stopped':True,'journal_tip':result.journal_tip}
            else:
                with _lease(selected_root):
                    if (_directory(selected_root) != plan.root_identity
                            or os.path.lexists(plan.destination)
                            or os.path.lexists(selected_root/PENDING_NAME)
                            or process.backend.active_named(plan.owner_name)):
                        raise TrainerSetupRecoveryRequired('The retained health owner still requires recovery.')
                    with process._held_lock:
                        if plan.run_id in process._held:
                            raise TrainerSetupRecoveryRequired('The held GPU owner requires process recovery.')
                    state = {'fingerprint':plan.fingerprint,'status':'not_started',
                             'tree_stopped':True,'journal_tip':''}
            self.append('health_reconciled',state)
            results.append(state)
        return tuple(results)
