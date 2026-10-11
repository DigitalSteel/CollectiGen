"""Explicit compiled host inputs over existing Manager acquisition/recovery.

No trust/feed default, enrollment, SDK replacement, second lease or updater.
Builds supply reviewed public trust, prior retained release and stock review.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import json
import os
import zipfile

from core.component_acquisition import ComponentAcquisition
from core.manager_update_service import ManagerUpdateService, ManagerUpdateHostBinding
from core.manager_self_update_recovery import ManagerRecovery, observe, digest, encoded, atomic
from core.manager_launch_owner import LaunchHold, _native_witness
from core.manager_stock_hook_provider import WindowsStockHookProvider
from core.manager_stock_apply import readback
from suite_core.discovery.component_staging import guard, require, MAX_METADATA_BYTES
from suite_core.discovery.component_release_signature import verify_component_release, verify_component_artifact, _json
from suite_core.discovery.manager_runtime_detached_admission import MANAGER, PROFILE, produce_detached_inventory


@dataclass(frozen=True)
class SealedLocalQualificationOpener:
    """Explicit test-build transport; exact GitHub URLs, sealed local bytes only.

    Only the stable test root embeds the completed response-map pin. A child binds
    it from its authenticated original root profile after channel admission, which
    avoids putting a target package's own hash into its compiled executable.
    """
    mapping_sha256: str | None = None
    root: Path | None = None
    prefix = 'sealed-local-qualification:'

    @property
    def transport_id(self):
        import re
        require(type(self.mapping_sha256) is str and re.fullmatch('[0-9a-f]{64}',self.mapping_sha256),
            'sealed_qualification_mapping_pin_required')
        return self.prefix+self.mapping_sha256

    def bind(self,root,transport_id):
        import re
        require(type(transport_id) is str and transport_id.startswith(self.prefix)
            and re.fullmatch('[0-9a-f]{64}',transport_id[len(self.prefix):]),
            'authenticated_qualification_transport_required')
        pin=transport_id[len(self.prefix):]
        require(self.mapping_sha256 is None or self.mapping_sha256==pin,'qualification_mapping_pin_changed')
        return SealedLocalQualificationOpener(pin,Path(root))

    def __call__(self,request,**options):
        import io,re
        from urllib.parse import urlsplit
        from suite_core.discovery.github_component_releases import REPOSITORY
        require(self.root is not None and self.root.is_absolute(),'bound_qualification_root_required')
        self.transport_id
        descriptor=self.root/'_bootstrap/qualification-responses.json';guard(descriptor)
        require(descriptor.stat().st_size<=MAX_METADATA_BYTES,'sealed_qualification_mapping_bounds')
        raw=descriptor.read_bytes()
        require(len(raw)<=MAX_METADATA_BYTES and sha256(raw).hexdigest()==self.mapping_sha256,
            'sealed_qualification_mapping_changed')
        document=_json(raw,'qualification responses')
        require(set(document)=={'schema','responses'} and type(document['schema']) is int and document['schema']==1
            and type(document['responses']) is dict and 1<=len(document['responses'])<=32,
            'closed_qualification_response_map_required')
        url=request.full_url
        listing={f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=50&page={page}' for page in (1,2,3)}
        parts=urlsplit(url)
        asset=(parts.scheme=='https' and parts.netloc=='github.com' and not parts.query and not parts.fragment
            and parts.path.startswith('/'+REPOSITORY+'/releases/download/collectigen.suite-manager%2Fv')
            and len(parts.path.split('/'))==7)
        require(request.get_method()=='GET' and (url in listing or asset)
            and url in document['responses'],'unknown_qualification_url')
        spec=document['responses'][url]
        require(type(spec) is dict and set(spec)=={'path','size','sha256'}
            and type(spec['path']) is str and type(spec['size']) is int and 0<spec['size']<=512*1024*1024
            and type(spec['sha256']) is str and re.fullmatch('[0-9a-f]{64}',spec['sha256']),
            'sealed_qualification_response_required')
        relative=Path(spec['path'])
        require(relative.parts and relative.parts[0]=='_qualification' and not relative.is_absolute()
            and '..' not in relative.parts and ':' not in spec['path'] and '\\' not in spec['path'],
            'qualification_response_scope_required')
        path=self.root/relative;guard(path);readback(path,spec['size'],spec['sha256'])
        offset=0;range_header=request.get_header('Range')
        if range_header is not None:
            require(type(range_header) is str and re.fullmatch(r'bytes=[0-9]+-',range_header),
                'qualification_range_required')
            offset=int(range_header[6:-1]);require(offset<spec['size'],'qualification_range_bound')
        class Response(io.BufferedReader):
            status=206 if range_header is not None else 200
            def getcode(self):return self.status
        stream=Response(path.open('rb'));stream.seek(offset)
        return stream


@dataclass(frozen=True)
class QualifiedStockReview:
    """Sealed stock binary/CLI evidence, separate from native owner permission."""
    evidence_bytes: bytes
    evidence_sha256: str

    def __call__(self, request, *, argv):
        from core.manager_stock_apply import fixed_argv
        require(type(self.evidence_bytes) is bytes and len(self.evidence_bytes)<=MAX_METADATA_BYTES
            and sha256(self.evidence_bytes).hexdigest()==self.evidence_sha256,'compiled_stock_review_evidence_changed')
        report=_json(self.evidence_bytes,'qualified stock report')
        require(report.get('status')=='stock_cli_native_qualified' and report.get('stock_version')=='1.2.161'
            and report.get('updater_sha256')==request.updater_sha256
            and report.get('platform')==dict(os='windows',architecture='x86_64')
            and report.get('hook_mode')=='All' and report.get('elevation_mode')=='none'
            and report.get('exact_full_package_apply') is True and report.get('prerequisites_verified') is True
            and report.get('evidence_refs'),'qualified_exact_stock_cli_report_required')
        require(argv==fixed_argv(request),'qualified_stock_exact_arguments_required')
        readback(request.update_exe,request.updater_size,request.updater_sha256)
        readback(request.package,request.package_size,request.package_sha256)
        # Original Recovery prepare/readback plus precreated native listeners
        # prove writable owned paths. This report never supplies closure/lease.
        journal=Path(request.suite_root)/'Recovery'/request.transaction_id/'transaction.json';guard(journal)
        require(journal.is_file(),'original_recovery_write_readback_required')
        return dict(status='stock_cli_native_reviewed',transaction_id=request.transaction_id,
            binding_sha256=request.binding_sha256,stock_version='1.2.161',hook_mode='All',
            elevation_mode='none',root_writable=True,prerequisites_ready=True,
            stock_evidence_sha256=self.evidence_sha256)


@dataclass(frozen=True)
class CompiledManagerUpdateProfile:
    trust_bytes: bytes
    trust_sha256: str
    channel: str
    transport_id: str
    opener: object
    initial_prior: dict
    updater_sha256: str
    native_review: object

    def check(self):
        require(type(self.trust_bytes) is bytes and len(self.trust_bytes)<=MAX_METADATA_BYTES
            and sha256(self.trust_bytes).hexdigest()==self.trust_sha256
            and self.channel in {'stable','beta'} and type(self.transport_id) is str
            and 0<len(self.transport_id)<=128 and callable(self.opener) and callable(self.native_review),
            'explicit_compiled_update_profile_required')
        require(type(self.updater_sha256) is str and len(self.updater_sha256)==64,
            'reviewed_stock_updater_pin_required')
        return self

    def service(self, admitted, version, cache, *, installed_selection=None, opener=None):
        self.check()
        require(cache.is_absolute() and cache.is_relative_to(admitted.layout.data_root/'Manager/UpdateCache')
            and cache!=admitted.layout.data_root/'Manager/UpdateCache','compiled_service_cache_scope_required')
        guard(cache);cache.mkdir(parents=True,exist_ok=False)
        acquisition=ComponentAcquisition(cache_root=cache,cache_owner=uuid4().hex,opener=self.opener if opener is None else opener,
            host_version=version,trust_bytes=self.trust_bytes,trust_sha256=self.trust_sha256)
        return ManagerUpdateService(acquisition,current_version=version,channel=self.channel,
            installed_selection=installed_selection)

    def root_host(self, *, owner, admitted, launcher):
        self.check()
        require(owner is launcher.owner and owner.root==admitted.layout.code_root,
            'original_compiled_root_host_required')
        launcher.successor_trust_sha256=self.trust_sha256
        version=launcher.active_manager_version
        zone=admitted.layout.data_root/'Manager/UpdateCache'
        root_cache=zone/('root-'+owner.record['owner']);child_cache=zone/('child-'+owner.record['owner'])
        opener=self.opener;transport_id=self.transport_id
        if type(opener) is SealedLocalQualificationOpener:
            require(transport_id=='sealed-local-qualification','explicit_test_build_transport_required')
            transport_id=opener.transport_id;opener=opener.bind(owner.root,transport_id)
        service=self.service(admitted,version,root_cache,installed_selection=launcher.current_successor_selection,opener=opener)
        host=ManagerUpdateHostBinding(service,transport_id,child_cache,apply_review=launcher.review_update_for_close)
        launcher.compiled_update_profile=self
        launcher.compiled_update_host=host
        launcher.compiled_update_admitted=admitted
        launcher.update_after_close=self.continue_after_close
        return host

    def child_service(self, *, session, admitted):
        from suite_core.manager.version import VERSION
        self.check();profile=session.update_profile
        require(profile is not None,'compiled_child_update_profile_changed')
        opener=self.opener;transport_id=self.transport_id
        if type(opener) is SealedLocalQualificationOpener:
            require(transport_id=='sealed-local-qualification','explicit_test_build_transport_required')
            transport_id=profile['transport_id'];opener=opener.bind(admitted.layout.code_root,transport_id)
        require(profile is not None and profile['current_version']==VERSION
            and profile['trust_sha256']==self.trust_sha256 and profile['channel']==self.channel
            and profile['transport_id']==transport_id,'compiled_child_update_profile_changed')
        cache=Path(profile['cache_root'])
        require(cache.is_relative_to(admitted.layout.data_root/'Manager/UpdateCache')
            and cache.name.startswith('child-'),'compiled_child_cache_scope_required')
        return self.service(admitted,VERSION,cache,opener=opener)

    def _release(self, root, source):
        release=json.loads(json.dumps(source))
        require(set(release)=={'version','channel','platform','files'} and release['channel']==self.channel,
            'compiled_prior_release_required')
        require(set(release['files'])=={'archive','manifest','signature','trust','proof','inventory','executable'}
            and release['platform']==dict(os='windows',architecture='x86_64'),'compiled_prior_release_scope_required')
        for spec in release['files'].values():
            relative=Path(spec['path'])
            require(not relative.is_absolute() and '..' not in relative.parts and ':' not in str(relative),
                'compiled_retained_relative_path_required')
            path=root/relative
            require(path.is_relative_to(root/'Recovery'),'compiled_prior_recovery_zone_required')
            guard(path);spec['path']=str(path)
        return release

    def _verify_release(self, release):
        files=release['files']
        def raw(name):
            require(files[name]['size']<=MAX_METADATA_BYTES,'compiled_release_metadata_bounds')
            readback(files[name]['path'],files[name]['size'],files[name]['sha256'])
            return Path(files[name]['path']).read_bytes()
        manifest,signature,trust,proof=(raw(name) for name in ('manifest','signature','trust','proof'))
        require(trust==self.trust_bytes,'compiled_prior_trust_changed')
        verified=verify_component_release(manifest,signature,trust,expected_component=MANAGER,
            expected_version=release['version'],expected_channel=self.channel,expected_os='windows',expected_architecture='x86_64')
        archive=Path(files['archive']['path']);verify_component_artifact(archive,verified)
        require(proof==produce_detached_inventory(archive,inventory_profile=PROFILE,
            expected_version=release['version'],expected_channel=self.channel)
            and sha256(proof).hexdigest()==verified.artifact.portable_manifest_sha256,'compiled_prior_signed_inventory_changed')
        rows={row['path']:row for row in _json(proof,'compiled prior inventory')['files']}
        require(files['executable']['sha256']==rows['CollectiGenManager.exe']['sha256']
            and files['executable']['size']==rows['CollectiGenManager.exe']['size_bytes']
            and files['inventory']['sha256']==sha256(proof).hexdigest(),'compiled_prior_retained_code_inventory_changed')
        return verified

    @staticmethod
    def _retain(path, raw):
        guard(path);path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            require(path.read_bytes()==raw,'retained_release_collision')
        else:
            with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        require(path.read_bytes()==raw,'retained_release_readback')
        return dict(path=str(path),size=len(raw),sha256=sha256(raw).hexdigest())

    def continue_after_close(self, launcher):
        """Physical A drain -> existing signed/recovery/native batch -> visible B."""
        self.check()
        review=getattr(launcher,'reviewed_update_for_close',None)
        if review is None:
            # Ordinary user close without Apply uses ordinary physical drain.
            receipt=launcher.route.finish_retained_normal_drain()
            launcher.normal_closure_receipt=receipt;launcher.state='closed'
            return receipt
        route,prepared,nonce,descriptor,owner_token=review
        owner=launcher.owner;host=launcher.compiled_update_host;admitted=launcher.compiled_update_admitted
        require(route is launcher.route and route.state=='maintenance_ready'
            and owner.fd==descriptor and owner.record['owner']==owner_token and launcher.state=='maintenance_ready',
            'exact_reviewed_post_drain_context_required')
        host.service.revalidate(prepared)
        transport=owner.native_transport
        transport.assert_accounted_live_scope()
        require(transport.job_active_count()==0,'compiled_update_original_scope_not_empty')
        prior=(launcher.current_successor_selection.revalidate() if launcher.current_successor_selection is not None else None)
        if prior is None:
            prior_release=self._release(owner.root,self.initial_prior)
        else:
            locator=json.loads((owner.root/'_bootstrap/manager-successor.json').read_bytes())
            journal=json.loads((owner.root/'Recovery'/locator['transaction']/'transaction.json').read_bytes())
            prior_release=journal['binding'][locator['release_side']]
        self._verify_release(prior_release)
        require(prior_release['version']==host.service.current_version,'compiled_current_prior_version_changed')
        current=owner.root/'Apps/Manager/current';updater=owner.root/'Apps/Manager/Update.exe'
        prior_proof=Path(prior_release['files']['proof']['path']).read_bytes()
        for row in _json(prior_proof,'compiled installed prior')['files']:
            path=current/row['path'];guard(path)
            require(observe(path)==dict(exists=True,size=row['size_bytes'],sha256=row['sha256']),
                'compiled_installed_prior_payload_changed')
        require(observe(updater)['sha256']==self.updater_sha256,'compiled_stock_updater_changed')
        readback(current/'CollectiGenManager.exe',expected=prior_release['files']['executable']['sha256'])
        package=owner.root/'Apps/Manager/packages'/prepared.artifact.name;guard(package)
        # Stock acquisition has already bounded/read-back this exact signed file.
        self._retain(package,prepared.artifact.read_bytes())
        transaction='manager-'+uuid4().hex
        retained=owner.root/'Recovery/Manager'/('retained-'+prepared.admission.artifact_sha256)/transaction
        with zipfile.ZipFile(prepared.artifact) as archive:
            executable=archive.read('lib/app/CollectiGenManager.exe')
        files={name:self._retain(retained/name,raw) for name,raw in dict(
            manifest=prepared.offer.manifest_bytes,signature=prepared.offer.signature_bytes,
            trust=self.trust_bytes,proof=prepared.proof,inventory=prepared.proof,executable=executable).items()}
        files['archive']=self._retain(retained/prepared.artifact.name,prepared.artifact.read_bytes())
        target=dict(version=prepared.admission.version,channel=self.channel,
            platform=dict(os='windows',architecture='x86_64'),files=files)
        healthy=self._retain(retained/'prior-healthy.json',encoded(dict(entry=route.wire.document['entry'],
            owner=owner_token,drain=owner.record['drain'],review_request=nonce)))
        repair=self._retain(retained/'repair-entry.json',encoded(dict(root=str(owner.root),
            settings=str(admitted.settings_path),initial_contract_sha256=launcher.initial_contract_sha256)))
        deployment=dict(root=str(owner.root),app_root=str(owner.root/'Apps/Manager'),current=str(current),
            packages=str(package.parent),update_exe=str(updater),manifest=str(current/'sq.version'),
            data=str(admitted.settings_path.parent),recovery=str(owner.root/'Recovery'),portable=True)
        binding_pin=[None]
        def gate(name,journal):
            owner._owned(allow_pending=name=='transaction_authority')
            require(journal['transaction']==transaction and journal['binding']['deployment']==deployment,
                'compiled_original_recovery_identity_changed')
            actual=digest(journal['binding'])
            if binding_pin[0] is None:
                require(name=='prepare_admission','compiled_recovery_initial_admission_required')
                self._verify_release(prior_release);host.service.revalidate(prepared)
                binding_pin[0]=actual
            require(actual==binding_pin[0],'compiled_original_recovery_binding_changed')
            if name in {'prepare_admission','exclusive_owner'} and not getattr(getattr(transport,'stock_hook_provider',None),'terminal_resources_closed',False):
                require(transport.job_active_count()==0,'compiled_recovery_exclusive_scope_required')
            return dict(status=name,transaction=transaction,binding_sha256=actual)
        recovery=ManagerRecovery(deployment,transaction,gates=gate)
        recovery.prepare(prior_release,target,healthy_receipt=healthy,repair_entry=repair,
            declared_data=[admitted.settings_path.name])
        fields=dict(transaction_id=transaction,suite_root=str(owner.root),manager_root=str(owner.root/'Apps/Manager'),
            packages_dir=str(package.parent),update_exe=str(updater),package=str(package),
            package_size=prepared.offer.release.artifact.size_bytes,package_sha256=prepared.admission.artifact_sha256,
            updater_size=updater.stat().st_size,updater_sha256=self.updater_sha256,
            current_executable_sha256=observe(current/'CollectiGenManager.exe')['sha256'],
            current_manifest_sha256=observe(current/'sq.version')['sha256'],current_version=host.service.current_version,
            target_version=prepared.admission.version,channel=self.channel,release_sha256=prepared.admission.release_sha256,
            trust_sha256=self.trust_sha256,inventory_sha256=prepared.admission.inventory_sha256,
            original_approval_sha256=sha256(nonce.encode()).hexdigest(),stale_token=uuid4().hex*2)
        def scope(**context):raise LaunchHold('original_native_provider_not_bound')
        def transaction_gate(request):
            journal=recovery.load()
            require(digest(journal['binding'])==binding_pin[0],'original_transaction_changed')
            return dict(status='original_transaction_current',transaction_id=transaction,
                binding_sha256=request.binding_sha256,durable_apply_intent_sha256=request.durable_apply_intent_sha256)
        request=launcher.prepare_update(host.service,prepared,fields,records=recovery,scope_verifier=scope,
            transaction_verifier=transaction_gate,recovery=recovery)
        provider=WindowsStockHookProvider(transport,request,recovery,admitted,environment=dict(os.environ))
        provider.prepare();provider.bind_scope()
        launcher.queue_update(native_review=self.native_review,create_process=provider.create_process)
        provider.resume_registered();provider.service_hooks()
        provider.member.process.wait(timeout=5)
        for session in provider.sessions.values():session.process.wait(timeout=5)
        provider.observe_closed()
        def payload(original,request):
            from core.manager_successor_admission import admit_successor_selection
            selection=admit_successor_selection(owner,recovery,request,release_side='target',
                argv=(str(current/'CollectiGenManager.exe'),*admitted.restart_arguments))
            recovery.check_data(recovery.load(),'expected_b')
            return dict(status='code_data_verified',transaction_id=transaction,binding_sha256=request.binding_sha256,
                target_version=selection.version,data_unchanged=True,current_executable_sha256=selection.executable_sha256,
                current_manifest_sha256=selection.manifest_sha256)
        launcher.finish_update(verify_payload=payload)
        provider.start_successor(launcher,(str(current/'CollectiGenManager.exe'),*admitted.restart_arguments))
        try:
            while provider.successor_route.state!='local_accepted':provider.successor_route.handle_next()
            provider.finish_successor_close()
            return launcher.retire_update(verify_terminal=provider.verify_terminal,settle_resources=provider.settle_terminal_resources)
        except BaseException as error:
            process=owner.children['replacement']
            if launcher.state!='update_target_health_pending' or process.poll() is None:raise
            launcher.error=error
            launcher.update_target_failed()
            return launcher
