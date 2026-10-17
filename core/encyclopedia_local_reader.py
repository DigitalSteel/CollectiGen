"""Explicit signed local Reader input; no network, admission, install or health."""
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
from uuid import uuid4
import argparse
from core.component_acquisition import ComponentOffer, AcquiredComponent, ComponentAcquisition
from core.application_update import ReleaseAssetSource
from core.encyclopedia_connection import COMPONENT
from suite_core.discovery.component_release_signature import verify_component_release, verify_component_artifact, MAX_METADATA_BYTES, _json
from suite_core.discovery.component_staging import guard, require
from suite_core.installation import _path

LOCAL_READER_FLAGS = ('--encyclopedia-local-reader-package', '--encyclopedia-local-reader-manifest', '--encyclopedia-local-reader-signature')

def lexical_local_path(value):
    path=Path(value)
    require(path.is_absolute() and '..' not in path.parts and len(str(path))<=4096
        and len(path.parts)<=128 and not str(path).startswith(('//', chr(92)*2)), 'explicit_local_reader_path_required')
    return path

@dataclass(frozen=True)
class LocalReaderInputs:
    package: Path
    manifest: Path
    signature: Path
    def __post_init__(self):
        paths=(self.package,self.manifest,self.signature)
        require(all(type(p) is type(Path()) and lexical_local_path(str(p))==p for p in paths), 'typed_local_reader_paths_required')
        require(len({str(p).casefold() for p in paths})==3, 'distinct_local_reader_inputs_required')

def parse_local_reader_arguments(arguments):
    arguments=tuple(arguments)
    if not arguments:return None
    require(all(sum(a.split('=',1)[0]==key for a in arguments)==1 for key in LOCAL_READER_FLAGS), 'exact_local_reader_arguments_required')
    parser=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    for key in LOCAL_READER_FLAGS:parser.add_argument(key,required=True)
    try:value=parser.parse_args(arguments)
    except SystemExit as error:raise ValueError('exact_local_reader_arguments_required') from error
    return LocalReaderInputs(*(lexical_local_path(getattr(value,key[2:].replace('-','_'))) for key in LOCAL_READER_FLAGS))

class SignedLocalReader:
    def __init__(self, inputs, acquisition):
        require(type(inputs) is LocalReaderInputs and type(acquisition) is ComponentAcquisition, 'original_local_reader_inputs_and_acquisition_required')
        require(acquisition.os=='windows' and acquisition.arch=='x86_64'
            and acquisition._expected_offer is None, 'original_normal_local_reader_acquisition_required')
        self.inputs,self.acquisition=inputs,acquisition
        self.offer=self.stamps=None
    def _read(self,path,maximum):
        guard(path)
        require(_path(path)==path and path.is_file() and path.stat().st_size<=maximum, 'bounded_unchanged_local_reader_file_required')
        before=path.stat()
        with path.open("rb") as stream:
            raw=stream.read(maximum+1)
        guard(path);after=path.stat()
        require(len(raw)<=maximum,"local_reader_file_bound")
        stamp=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        require(stamp(before)==stamp(after) and len(raw)==after.st_size,'local_reader_input_changed')
        return raw,stamp(after)+(sha256(raw).hexdigest(),)
    def verify(self,channel):
        self.acquisition._trust();self.acquisition._check(None)
        message,m=self._read(self.inputs.manifest,MAX_METADATA_BYTES)
        signature,s=self._read(self.inputs.signature,MAX_METADATA_BYTES)
        package,p=self._read(self.inputs.package,self.acquisition.maximum)
        document=_json(message,'local Reader manifest')
        release=verify_component_release(message,signature,self.acquisition.trust,expected_component=COMPONENT,
            expected_version=document['version'],
            expected_channel=channel,expected_os='windows',expected_architecture='x86_64')
        verify_component_artifact(self.inputs.package,release)
        require(sha256(package).hexdigest()==release.artifact.sha256,'local_reader_artifact_changed')
        offer=ComponentOffer(release,'install',document['release_notes'],message,signature,
            self.inputs.manifest.as_uri(),self.inputs.signature.as_uri(),
            ReleaseAssetSource(release.artifact.name,self.inputs.package.as_uri(),len(package)),
            self.acquisition.trust_pin,None,locator=None,repository_id=None)
        return offer,(m,s,p)
    def discover(self,channel):
        self.offer=self.stamps=None
        self.offer,self.stamps=self.verify(channel)
        return (self.offer,)
    def revalidate(self,offer):
        require(offer is self.offer and self.stamps is not None,'current_local_reader_offer_required')
        current,stamps=self.verify(offer.release.channel)
        require(current==offer and stamps==self.stamps,'local_reader_input_changed')
    def acquire(self,offer,*,progress=None,should_cancel=None):
        self.revalidate(offer);self.acquisition._check(should_cancel)
        require(not self.inputs.package.is_relative_to(self.acquisition.root),'local_reader_source_cache_overlap')
        used=0
        for path in self.acquisition.root.rglob('*'):
            guard(path)
            if path.is_file():used+=path.stat().st_size
        require(used+offer.release.artifact.size_bytes+1024<=self.acquisition.cache_maximum,'local_reader_cache_bound')
        attempt=self.acquisition.root/('local-reader-'+uuid4().hex);guard(attempt);attempt.mkdir(exist_ok=False)
        marker=attempt/'INCOMPLETE.json';marker.write_text('{"purpose":"LOCAL_READER_COPY_INCOMPLETE"}')
        destination=attempt/offer.release.artifact.name
        copied=0
        guard(self.inputs.package)
        with self.inputs.package.open('rb') as source,destination.open('xb') as target:
            while True:
                self.acquisition._check(should_cancel)
                chunk=source.read(1024*1024)
                if not chunk:break
                copied+=len(chunk)
                require(copied<=offer.release.artifact.size_bytes,'local_reader_copy_bound')
                target.write(chunk)
                if progress:
                    progress("bytes", "Copying the reader app", copied, offer.release.artifact.size_bytes)
        self.revalidate(offer);self.acquisition._check(should_cancel)
        verify_component_artifact(destination,offer.release)
        marker.unlink()  # Only this exact completed attempt's marker.
        return AcquiredComponent(offer,destination)
