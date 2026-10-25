"""Isolated read-only cached binding; explicit paths/pins, no network or trust defaults."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from suite_core.discovery.component_staging import read_metadata_bytes
from suite_core.discovery.component_release_signature import verify_component_release
from suite_core.installation import ComponentId
from core.organizer_connection_settings import OrganizerConnectionStore
from suite_core.manager.organizer_connection_ui import OrganizerConnectionHost

@dataclass(frozen=True)
class CachedOrganizerBinding:
    manifest_path: Path
    signature_path: Path
    trust_path: Path
    manifest_sha256: str
    signature_sha256: str
    trust_sha256: str
    version: str

    def __post_init__(self):
        for path in (self.manifest_path,self.signature_path,self.trust_path):
            if type(path) is not Path and not isinstance(path,Path):raise ValueError('explicit_cached_path_required')
            if not path.is_absolute():raise ValueError('absolute_cached_path_required')
        for pin in (self.manifest_sha256,self.signature_sha256,self.trust_sha256):
            if type(pin) is not str or re.fullmatch('[0-9a-f]{64}',pin) is None:raise ValueError('explicit_cached_pin_required')

def load_cached_inputs(binding):
    if binding is None:return None,'cache_unconfigured'
    if type(binding) is not CachedOrganizerBinding:raise ValueError('typed_cached_organizer_binding_required')
    try:
        paths=(binding.manifest_path,binding.signature_path,binding.trust_path)
        pins=(binding.manifest_sha256,binding.signature_sha256,binding.trust_sha256)
        raw=[]
        for path,pin in zip(paths,pins):
            data=read_metadata_bytes(path)
            if sha256(data).hexdigest()!=pin:return None,'cached_metadata_changed'
            raw.append(data)
        manifest,signature,trust=raw
        verify_component_release(manifest,signature,trust,
            expected_component=ComponentId('collectigen.media-organizer'),expected_version=binding.version,
            expected_channel='beta',expected_os='windows',expected_architecture='x86_64')
        return dict(manifest_bytes=manifest,signature_bytes=signature,trust_bytes=trust,
                    expected_version=binding.version),'cached_signature_verified'
    except FileNotFoundError:return None,'cached_metadata_missing'
    except PermissionError:return None,'cached_metadata_unreadable'
    except OSError:return None,'cached_metadata_unreadable'
    except ValueError:return None,'cached_identity_unavailable'

class CachedOrganizerFactory:
    """Callable matches existing main(organizer_connection_factory=...) seam.

    Paths/pins must come from the retained acquisition/approved cache owner, not
    directory scanning or settings assertions. Every call rechecks exact bytes.
    No cache creation, package read, installed state, settings read or download.
    """
    def __init__(self,binding,*,executor_factory=None,clock=None):
        if binding is not None and type(binding) is not CachedOrganizerBinding:raise ValueError('typed_cached_organizer_binding_required')
        self.binding=binding;self.executor_factory=executor_factory;self.clock=clock
        self.last_state='not_observed'

    def __call__(self,settings_path):
        inputs,self.last_state=load_cached_inputs(self.binding)
        return OrganizerConnectionHost(store=OrganizerConnectionStore(settings_path),
            publisher_inputs=inputs,executor_factory=self.executor_factory,clock=self.clock)


@dataclass(frozen=True)
class CompletedOrganizerSelection:
    """Producer-issued selected paths/receipt; never root trust authority."""
    directory: Path
    receipt: dict
    trust_bytes: bytes
    root_trust_sha256: str
    version: str


class MetadataPresentation(str):
    """String-compatible result retaining the original owner's capability."""
    def __new__(cls,state,can_refresh):
        if type(state) is not str or type(can_refresh) is not bool:raise ValueError('finite_metadata_presentation_required')
        value=super().__new__(cls,state);value.can_refresh=can_refresh;return value


class CompletedOrganizerFactory:
    """Original producer callbacks; no locator guessing or network fallback."""
    def __init__(self,observe_completed,*,refresh_completed=None,observe_status=None,executor_factory=None,clock=None):
        if not callable(observe_completed):raise ValueError('completed_metadata_observer_required')
        if refresh_completed is not None and not callable(refresh_completed):raise ValueError('completed_metadata_refresh_required')
        self.observe_completed=observe_completed;self.refresh_completed=refresh_completed
        self.observe_status=observe_status
        self.executor_factory=executor_factory;self.clock=clock;self.last_state='not_observed'

    def _inputs(self,selection):
        from core.organizer_completed_metadata import read_completed_inputs
        if selection is None:return None,'completed_metadata_missing'
        if type(selection) is not CompletedOrganizerSelection:raise ValueError('typed_completed_selection_required')
        return read_completed_inputs(selection.directory,selection.receipt,
            trust_bytes=selection.trust_bytes,root_trust_sha256=selection.root_trust_sha256,
            expected_version=selection.version)

    def observe(self):
        presentation=self.owner_presentation()
        if not presentation.can_refresh and self.observe_status is not None:
            return None,presentation
        try:selection=self.observe_completed()
        except OSError:return None,MetadataPresentation('completed_metadata_unreadable',False)
        inputs,state=self._inputs(selection)
        latest=self.owner_presentation()
        if not latest.can_refresh and self.observe_status is not None:return None,latest
        return inputs,MetadataPresentation(state if inputs is not None else str(latest),latest.can_refresh)

    def owner_presentation(self):
        if self.observe_status is None:return MetadataPresentation('completed_metadata_missing',self.refresh_completed is not None)
        try:status=self.observe_status()
        except OSError:return MetadataPresentation('completed_metadata_unreadable',False)
        return MetadataPresentation(status.state,status.can_refresh)

    def refresh(self,cancel):
        from core.application_update import UpdateServiceError
        if self.refresh_completed is None:return self.observe()
        try:
            selection=self.refresh_completed(cancel)
            inputs,state=self._inputs(selection)
            if cancel():raise InterruptedError('metadata_refresh_cancelled')
            if inputs is not None:
                latest=self.owner_presentation()
                return (None,latest) if not latest.can_refresh else (inputs,MetadataPresentation(state,latest.can_refresh))
        except InterruptedError:raise
        except (OSError,UpdateServiceError):pass
        # Failed refresh may retain only data that still verifies with CURRENT
        # independently root-bound trust, never an old in-memory trust snapshot.
        inputs,state=self.observe()
        if not state.can_refresh:return inputs,state
        return inputs,MetadataPresentation('refresh_unavailable_cached_verified' if inputs is not None else 'refresh_unavailable',state.can_refresh)

    def __call__(self,settings_path):
        inputs,self.last_state=self.observe()
        return OrganizerConnectionHost(store=OrganizerConnectionStore(settings_path),publisher_inputs=inputs,
            executor_factory=self.executor_factory,clock=self.clock,
            metadata_refresh=self.refresh if self.refresh_completed is not None else None,
            metadata_state=self.last_state,metadata_can_refresh=self.last_state.can_refresh)
