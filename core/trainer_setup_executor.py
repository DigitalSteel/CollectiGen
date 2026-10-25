"""Confirmed trainer operations through the shared Setup queue and recovery owner."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from .trainer_environment import (TrainerEnvironmentService, TrainerProfile, TrainerGeneration,
    TrainerEnvironmentStatus, _read_json, _write_json, _decode)
from .trainer_environment_pack import TrainerEnvironmentPack
from .trainer_runtime_health import TrainerRuntimeHealthService
from .trainer_runtime_catalog import RuntimeSoftwareCatalog
from .trainer_runtime_acquisition import RuntimeAcquisitionPlan, RuntimeAcquisition, TrainerRuntimeAcquisition
from .trainer_runtime_assembly import TrainerRuntimeAssembly
from .trainer_setup_recovery import TrainerSetupRecovery, TrainerSetupRecoveryRequired
from .training_process import ProcessInput, TrainingProcessService, _bytes, _read
from .training_file_inventory import _check_path, _control, MAX_FILES
from .training_source_io import _assert_unredirected

ACTIONS = frozenset({'managed','existing','import','export','repair','activate','rollback'})
MAX_REVIEW_BYTES = 256 * 1024
DEFAULT_STORAGE_BYTES = 32 * 1024**3


def _json(value):
    if isinstance(value,Path):return str(value)
    if isinstance(value,dict):return {key:_json(item) for key,item in value.items()}
    if isinstance(value,(tuple,list)):return [_json(item) for item in value]
    return value


def _absolute(value):
    if type(value) is not str or not value or len(value)>4096 or '\0' in value or not Path(value).is_absolute():
        raise ValueError('Select an absolute trainer Setup location.')
    return str(Path(value).absolute())


class _ExecutionLease:
    _locked = TrainerEnvironmentService._locked
    def __init__(self,root):self.root=Path(root)


class TrainerSetupExecutor:
    """No IO at construction; preview and execution run in coordinated workers.

    Installed profile/catalog paths are distribution inputs. Selection and
    activation remain separate from software installation and training launch.
    """
    def __init__(self,jobs,root,*,profile_path,catalog_path,source_root,
                 storage_budget_bytes=DEFAULT_STORAGE_BYTES,process=None):
        if type(storage_budget_bytes) is not int or not 0<storage_budget_bytes<=64*1024**3:
            raise ValueError('Invalid trainer storage allowance.')
        self.jobs=jobs;self.root=Path(root).absolute()
        self.profile_path=Path(profile_path).absolute();self.catalog_path=Path(catalog_path).absolute()
        self.source_root=Path(source_root).absolute();self.storage_budget_bytes=storage_budget_bytes
        self.process=process if process is not None else TrainingProcessService()
        self.managed_root=self.root/'managed';self.health_root=self.root/'health'
        self.review_root=self.root/'reviews'

    def profile(self):
        return TrainerProfile.from_dict(_read_json(self.profile_path))

    def _location(self,path):
        path=Path(path).absolute();_assert_unredirected(path)
        path.mkdir(parents=True,exist_ok=True);_check_path(path,directory=True)
        return path

    def prepare(self,action,selection,*,should_cancel=None,progress=None):
        if action not in ACTIONS:raise ValueError('Unsupported trainer Setup operation.')
        _control(should_cancel,time.monotonic()+60)
        profile=self.profile();inputs=[asdict(ProcessInput.capture(self.profile_path,should_cancel))]
        if action=='managed':
            if selection is not None:raise ValueError('Managed setup uses the reviewed distribution sources.')
            catalog=RuntimeSoftwareCatalog.from_dict(_read_json(self.catalog_path))
            inputs.append(asdict(ProcessInput.capture(self.catalog_path,should_cancel)))
            plan=catalog.installation_plan(destination=self.root/'downloads',candidate_workspace=self.root/'candidates',
                source_root=self.source_root,storage_budget_bytes=self.storage_budget_bytes,
                storage_scope='Only the selected managed trainer software, downloads and recovery records.')
            selection=plan.payload()
        elif action in {'activate','rollback'}:
            if type(selection) is not str or not re.fullmatch(r'[0-9a-f]{32}',selection):
                raise ValueError('Select an exact retained trainer generation.')
            TrainerEnvironmentService(self.managed_root,profile).verify_generation(selection,cancel=should_cancel)
        elif action=='export':
            if not isinstance(selection,(tuple,list)) or len(selection)!=2 or not re.fullmatch(r'[0-9a-f]{32}',str(selection[0])):
                raise ValueError('Select a generation and a new pack destination.')
            selection=[selection[0],_absolute(selection[1])]
            TrainerEnvironmentService(self.managed_root,profile).verify_generation(selection[0],cancel=should_cancel)
            if os.path.lexists(selection[1]):raise ValueError('Trainer packs never overwrite an existing destination.')
        else:
            selection=_absolute(selection)
            if action=='import':
                TrainerEnvironmentPack(TrainerEnvironmentService(self.managed_root,profile)).inspect(selection,cancel=should_cancel)
                inputs.append(asdict(ProcessInput.capture(selection,should_cancel)))
        pointer=self.managed_root/'active.json'
        _assert_unredirected(pointer)
        active_sha=hashlib.sha256(_read(pointer)).hexdigest() if os.path.lexists(pointer) else ''
        value={'schema_version':1,'kind':'confirmed_trainer_setup_review','action':action,'selection':selection,
            'root':str(self.root),'profile_sha256':profile.sha256,'inputs':_json(inputs),
            'active_sha256':active_sha,'storage_budget_bytes':self.storage_budget_bytes}
        raw=_bytes(value)
        if len(raw)>MAX_REVIEW_BYTES:raise ValueError('The trainer Setup review exceeds its bound.')
        root=self._location(self.review_root);path=root/(uuid4().hex+'.json')
        _write_json(path,value)
        return {'review_path':str(path),'review_sha256':hashlib.sha256(raw).hexdigest()},value

    def _review(self,reference,should_cancel):
        if set(reference)!={'review_path','review_sha256'}:raise ValueError('Invalid queued trainer review.')
        path=Path(_absolute(reference['review_path']))
        if path.parent!=self.review_root or not re.fullmatch(r'[0-9a-f]{32}\.json',path.name):
            raise ValueError('The queued trainer review is outside its owned review store.')
        raw=_read(path,limit=MAX_REVIEW_BYTES)
        if hashlib.sha256(raw).hexdigest()!=reference['review_sha256']:
            raise ValueError('The confirmed trainer Setup review changed.')
        value=_decode(raw)
        fields={'schema_version','kind','action','selection','root','profile_sha256','inputs','active_sha256','storage_budget_bytes'}
        if (type(value) is not dict or set(value)!=fields or value['schema_version']!=1
                or type(value['schema_version']) is not int
                or value['kind']!='confirmed_trainer_setup_review' or value['action'] not in ACTIONS
                or value['root']!=str(self.root) or value['storage_budget_bytes']!=self.storage_budget_bytes
                or type(value['inputs']) is not list or not 1<=len(value['inputs'])<=2):
            raise ValueError('The queued trainer Setup contract is invalid.')
        expected=[self.profile_path]
        if value['action']=='managed':expected.append(self.catalog_path)
        if value['action']=='import':expected.append(Path(_absolute(value['selection'])))
        if [Path(row['path']) for row in value['inputs']]!=expected:
            raise ValueError('The reviewed trainer inputs changed.')
        for row in value['inputs']:
            saved=ProcessInput(**{**row,'state':tuple(row['state'])})
            if ProcessInput.capture(saved.path,should_cancel)!=saved:
                raise ValueError('A reviewed trainer input changed. Prepare a new Setup review.')
        profile=self.profile()
        if profile.sha256!=value['profile_sha256']:raise ValueError('The reviewed trainer profile changed.')
        return value,profile

    def storage_usage(self, should_cancel=None):
        """Count explicit software-store metadata with cancellation and a deadline.

        Directory enumeration supplies file sizes; payload identities are checked
        separately by installation and health, never opened by this accounting.
        """
        deadline = time.monotonic() + 60
        _control(should_cancel, deadline)
        _assert_unredirected(self.root)
        if not self.root.exists():
            return 0
        total = count = 0
        pending = [self.root]
        while pending:
            _control(should_cancel, deadline)
            directory = pending.pop()
            before = _check_path(directory, directory=True)
            with os.scandir(directory) as entries:
                for entry in entries:
                    _control(should_cancel, deadline)
                    count += 1
                    if count > MAX_FILES:
                        raise ValueError('Trainer storage exceeds its bounded file count.')
                    state = entry.stat(follow_symlinks=False)
                    if int(getattr(state, 'st_file_attributes', 0)) & (0x400 | 0x1000 | 0x40000 | 0x400000):
                        raise ValueError('Trainer storage contains a redirected or unavailable entry.')
                    if stat.S_ISDIR(state.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(state.st_mode):
                        total += state.st_size
                    else:
                        raise ValueError('Trainer storage contains an unsupported filesystem entry.')
            if _check_path(directory, directory=True) != before:
                raise ValueError('Trainer storage changed while its usage was measured. Retry Setup.')
        return total

    def _storage_accounting(self, should_cancel):
        """Sample shared-store growth once per second during streamed writes.

        The acquisition budget independently accounts for every reserved/written
        byte and checks physical free space and cancellation on every chunk.
        This sampler bounds repeated directory walks; each phase starts fresh.
        """
        observed = None
        measured_at = 0.0
        def usage():
            nonlocal observed, measured_at
            _control(should_cancel, time.monotonic() + 60)
            if observed is None or time.monotonic() - measured_at >= 1.0:
                current = self.storage_usage(should_cancel)
                observed = current if observed is None else max(observed, current)
                measured_at = time.monotonic()
            return observed
        return usage

    def __call__(self,operation,*,should_cancel,progress):
        reference=dict(operation.params);value,profile=self._review(reference,should_cancel)
        operation_root=self.root/'operations'/reference['review_sha256']
        with _ExecutionLease(self.root/'operation-owner')._locked():
            recovery=TrainerSetupRecovery(operation_root/'events',reference['review_sha256'])
            self._location(self.health_root)
            recovery.reconcile_health(self.health_root,process=self.process)
            rows,_=recovery.snapshot()
            complete=next((row['details'] for row in reversed(rows) if row['event']=='complete'),None)
            if complete is not None:
                self._verify_completed(value,profile,complete,should_cancel)
                return complete['message']
            pointer=self.managed_root/'active.json';_assert_unredirected(pointer)
            current=hashlib.sha256(_read(pointer)).hexdigest() if os.path.lexists(pointer) else ''
            if current!=value['active_sha256']:
                raise ValueError('The active trainer changed after review. Confirm the current Setup state again.')
            _control(should_cancel,time.monotonic()+60)
            def health(root,checked_profile):
                result=TrainerRuntimeHealthService(self.jobs,self.health_root,process=self.process,
                    should_cancel=should_cancel,timeout_seconds=600,retain_plan=recovery.retain_health).check(root,checked_profile)
                recovery.append('health_finished',{'software_root':str(root),'status':result.status,
                    'receipt_path':str(result.receipt_path) if result.receipt_path else '',
                    'receipt_sha256':result.receipt_sha256,
                    'outcome':_json(asdict(result.outcome)) if result.outcome else None})
                if result.status == 'cancelled' and (result.outcome is None or result.outcome.tree_stopped is True):
                    raise InterruptedError('Trainer health check cancelled; recovery evidence was retained.')
                if result.status!='healthy' or not result.health.healthy:
                    raise TrainerSetupRecoveryRequired('Trainer health is unverified. Retained evidence must be recovered before retry.')
                return result.health
            service=TrainerEnvironmentService(self.managed_root,profile,health_probe=health,
                retain_stage=lambda stage,sha:recovery.retain_stage(self.managed_root,stage,sha))
            action,selection=value['action'],value['selection']
            progress(0,0,'Preparing the confirmed trainer operation…')
            recovery.append('operation_started',{'action':action})
            try:
                # A crash after stage promotion must not create another copy.
                retained=None
                if action in {'managed','import','repair'}:
                    for row in reversed(rows):
                        if row['event']!='stage_started':continue
                        details=row['details'];identity=details['generation_id']
                        final=service._generation_path(identity);stage=self.managed_root/'staging'/identity
                        if details!={'generation_id':identity,'stage':str(stage),'final':str(final),'profile_sha256':profile.sha256}:
                            raise TrainerSetupRecoveryRequired('The retained stage binding changed.')
                        if final.exists() and stage.exists():raise TrainerSetupRecoveryRequired('Both sides of trainer promotion exist; preserve them for recovery.')
                        if final.exists():retained=service.verify_generation(identity,cancel=should_cancel)
                        break
                reserve=(profile.byte_count if retained is None and action in {'managed','import','repair'} else 0)+64*1024**2
                if self.storage_usage(should_cancel)+reserve>self.storage_budget_bytes:
                    raise ValueError('The reviewed trainer storage allowance is insufficient; retained software is preserved.')
                if retained is not None:result=retained
                elif action=='managed':
                    source=self._managed_source(value,profile,recovery,should_cancel,progress)
                    # Downloads and assembly consumed space after the initial review.
                    if self.storage_usage(should_cancel) + reserve > self.storage_budget_bytes:
                        raise ValueError('Trainer installation needs more space; verified downloads and assembly are retained.')
                    result=service.install(source,cancel=should_cancel)
                elif action=='existing':result=service.inspect_existing(selection,cancel=should_cancel)
                elif action=='import':result=TrainerEnvironmentPack(service).import_pack(selection,cancel=should_cancel)
                elif action=='export':result=TrainerEnvironmentPack(service).export(selection[0],selection[1],cancel=should_cancel)
                elif action=='repair':result=service.repair(selection,cancel=should_cancel)
                else:result=getattr(service,action)(selection,cancel=should_cancel)
                if isinstance(result,TrainerEnvironmentStatus) and (result.state!='healthy' or result.generation is None):
                    raise TrainerSetupRecoveryRequired(result.details)
                message=('Trainer software pack exported and verified.' if action=='export' else
                    'Trainer environment selected after a fresh health check.' if action in {'activate','rollback'} else
                    'Trainer software verified. Review activation and training resources in Setup.')
                if action in {'managed','import','repair'} and self.storage_usage(should_cancel) > self.storage_budget_bytes:
                    raise ValueError('Trainer storage exceeds the reviewed allowance; the verified generation is retained.')
                record={'action':action,'message':message,'result':_json(asdict(result))}
                recovery.append('complete',record)
                return message
            except Exception as exc:
                recovery.append('failed',{'action':action,'error_type':type(exc).__name__,
                    'cancelled':isinstance(exc,InterruptedError)})
                raise

    def _verify_completed(self,value,profile,complete,should_cancel):
        if complete['action']!=value['action']:raise TrainerSetupRecoveryRequired('The retained Setup result changed.')
        result=complete['result'];service=TrainerEnvironmentService(self.managed_root,profile)
        if value['action']=='export':
            actual=TrainerEnvironmentPack(service).inspect(value['selection'][1],cancel=should_cancel)
            if _json(asdict(actual))!=result:raise TrainerSetupRecoveryRequired('The retained exported pack changed.')
        elif value['action']=='existing':
            from .trainer_environment import _verify_software,software_phase_seconds
            generation=result['generation']
            if _verify_software(Path(value['selection']),profile,should_cancel,time.monotonic()+software_phase_seconds(profile))!=generation['health_inventory_sha256']:
                raise TrainerSetupRecoveryRequired('The selected existing trainer changed.')
        else:
            actual=service.verify_generation(result['generation_id'],cancel=should_cancel)
            if str(actual.software_root)!=result['software_root'] or actual.health_inventory_sha256!=result['health_inventory_sha256']:
                raise TrainerSetupRecoveryRequired('The completed trainer generation changed.')
            if value['action'] in {'activate','rollback'} and service.status(cancel=should_cancel).generation!=actual:
                raise TrainerSetupRecoveryRequired('The active trainer changed after this operation.')

    def _managed_source(self,value,profile,recovery,should_cancel,progress):
        plan=RuntimeAcquisitionPlan.from_dict(value['selection'])
        catalog=RuntimeSoftwareCatalog.from_dict(_read_json(self.catalog_path));catalog.validate_plan(plan)
        expected=catalog.installation_plan(destination=self.root/'downloads',candidate_workspace=self.root/'candidates',
            source_root=self.source_root,storage_budget_bytes=self.storage_budget_bytes,
            storage_scope='Only the selected managed trainer software, downloads and recovery records.')
        if plan.fingerprint!=expected.fingerprint:raise ValueError('The managed trainer acquisition locations changed.')
        rows,_=recovery.snapshot()
        assemblies=[row['details'] for row in rows if row['event']=='assembly_complete']
        if assemblies:
            row=assemblies[-1];root=Path(row['root'])
            if root.parent!=self.root/'candidates' or _read_json(root/'profile.json')!=profile.payload():
                raise TrainerSetupRecoveryRequired('The retained trainer assembly changed.')
            return root/'software'
        attempts=[row['details'] for row in rows if row['event']=='acquisition_started']
        retry=None
        if attempts:
            row=attempts[-1];retry=Path(row['attempt'])
            if retry.parent!=Path(plan.payload()['destination']) or row['plan_fingerprint']!=plan.fingerprint:
                raise TrainerSetupRecoveryRequired('The retained download attempt changed.')
        if retry is not None and (retry/'complete.json').exists():
            acquisition=RuntimeAcquisition(plan.fingerprint,retry,tuple((row['filename'],str(retry/row['filename'])) for row in plan.artifacts))
        else:
            acquisition=TrainerRuntimeAcquisition(plan).acquire(confirmed_fingerprint=plan.fingerprint,
                storage_usage=self._storage_accounting(should_cancel),retry_from=retry,cancel=should_cancel,
                retain_attempt=recovery.retain_acquisition,
                progress=lambda row:progress(row.get('completed',0),row.get('total',0),'Downloading and verifying reviewed trainer files…'))
        destination=self.root/'candidates'/('assembly-'+uuid4().hex)
        recovery.append('assembly_started',{'root':str(destination),'plan_fingerprint':plan.fingerprint})
        assembly=TrainerRuntimeAssembly(plan).assemble(acquisition,destination,source_root=self.source_root,
            confirmed_fingerprint=plan.fingerprint,storage_usage=self._storage_accounting(should_cancel),catalog=catalog,
            cancel=should_cancel,progress=lambda *_:progress(0,0,'Assembling the separate trainer environment…'))
        if self.storage_usage(should_cancel) > self.storage_budget_bytes:
            raise ValueError('Trainer storage grew beyond the reviewed allowance; the assembly is retained.')
        if assembly.profile.sha256!=profile.sha256:raise ValueError('The assembled runtime differs from its qualified profile.')
        recovery.append('assembly_complete',{'root':str(destination),'profile_sha256':profile.sha256})
        return assembly.software_root


    def completed_result(self,reference,*,should_cancel=None):
        """Background read-back for the originating dialog or queue history."""
        value,profile=self._review(dict(reference),should_cancel)
        recovery=TrainerSetupRecovery(self.root/'operations'/reference['review_sha256']/'events',reference['review_sha256'])
        rows,_=recovery.snapshot()
        complete=next((row['details'] for row in reversed(rows) if row['event']=='complete'),None)
        if complete is None:raise TrainerSetupRecoveryRequired('This Setup operation has no verified completed result.')
        self._verify_completed(value,profile,complete,should_cancel)
        raw=complete['result']
        if value['action']=='export':
            from .trainer_environment_pack import TrainerPackInfo
            return TrainerPackInfo(**raw)
        from .trainer_environment import TrainerHealth
        def generation(row):
            return TrainerGeneration(**{**row,'software_root':Path(row['software_root']),
                'health':TrainerHealth(**row['health'])})
        if value['action']=='existing':
            return TrainerEnvironmentStatus(**{**raw,'generation':generation(raw['generation'])})
        return generation(raw)


    def save_resources(self,reference,selection_path,tokenizers_root,output_root,expected_revision,*,should_cancel=None):
        """Bind chosen folders to final-location health before saving Setup."""
        from .training_setup_selection import TrainingSetupSelectionService,SoftwareInventoryBinding
        from .training_run_recovery import _plan
        from .training_process import TrainingProcessResult
        value,profile=self._review(dict(reference),should_cancel)
        if value['action'] not in {'activate','rollback','existing'}:
            raise ValueError('Activate or verify this environment before saving training resources.')
        result=self.completed_result(reference,should_cancel=should_cancel)
        generation=result.generation if isinstance(result,TrainerEnvironmentStatus) else result
        recovery=TrainerSetupRecovery(self.root/'operations'/reference['review_sha256']/'events',reference['review_sha256'])
        rows,_=recovery.snapshot()
        recorded=next((row['details'] for row in reversed(rows) if row['event']=='health_finished'
            and row['details'].get('software_root')==str(generation.software_root)),None)
        if recorded is None or recorded.get('status')!='healthy' or not recorded.get('receipt_path'):
            raise TrainerSetupRecoveryRequired('Fresh health evidence at the selected software location is unavailable.')
        receipt_path=Path(recorded['receipt_path'])
        if receipt_path.name!='installation-health-receipt.json' or receipt_path.parent.parent!=self.health_root:
            raise TrainerSetupRecoveryRequired('The selected health receipt is outside its reviewed workspace.')
        receipt=_read_json(receipt_path,limit=2*1024**2);plan=_plan(receipt['plan'])
        if receipt_path!=plan.destination/'installation-health-receipt.json':
            raise TrainerSetupRecoveryRequired('The selected health receipt belongs to another process.')
        outcome=TrainingProcessResult(**{**recorded['outcome'],'destination':Path(recorded['outcome']['destination'])})
        TrainerRuntimeHealthService(self.jobs,self.health_root,process=self.process,
            should_cancel=should_cancel).verify_retained(generation.software_root,profile,plan,
                receipt['request'],outcome,recorded['receipt_sha256'])
        store=TrainingSetupSelectionService(selection_path)
        if store.snapshot()[1]!=expected_revision:raise ValueError('Saved training Setup changed. Review the selected folders again.')
        if value['action']=='existing':
            inventories=receipt['request']['process']['inventories']
            by_role={row['purpose']:SoftwareInventoryBinding(row['root'],row['manifest_path'],row['manifest_sha256']) for row in inventories}
            selected=store.prepare_existing(profile,by_role['source_inventory'],by_role['environment_inventory'],
                by_role['python_base_inventory'],generation.software_root/profile.payload()['python']['executable'],
                tokenizers_root,output_root,should_cancel=should_cancel)
        else:
            selected=store.prepare_managed(TrainerEnvironmentService(self.managed_root,profile),profile,
                generation.generation_id,tokenizers_root,output_root,should_cancel=should_cancel)
        revision=store.save(selected,expected_revision)
        recovery.append('resources_saved',{'selection_path':str(Path(selection_path).absolute()),'revision':revision,
            'health_receipt_path':str(receipt_path),'health_receipt_sha256':recorded['receipt_sha256'],
            'generation_id':generation.generation_id})
        return revision
