"""Source-only first-install adapter; never constructs or forwards an entry.

The original owner issues a precreation capability. The stock installer creates
origin/journals and publishes settings; this module neither fabricates baseline
evidence nor supplies native health. Compiled profiles remain inactive.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import sys

from core.component_acquisition import ComponentAcquisition
from core.encyclopedia_connection import COMPONENT, ReaderConnectionStore
from core.encyclopedia_installation import EncyclopediaInstallation
from core.encyclopedia_update_binding import admit_standalone_binding
from core.manager_launch_owner import DeploymentOwner
from release_contract import sha256_file
from suite_core.installation import _path
from suite_core.discovery.component_staging import ComponentStageAdmission, MAX_ARCHIVE, canonical, component_origin, guard, require


@dataclass(frozen=True)
class EncyclopediaEnrollmentContext:
    root: Path
    data_root: Path
    settings_path: Path
    bootstrap_sha256: str
    trust_bytes: bytes
    trust_sha256: str

    @property
    def identity(self):
        return sha256(canonical({'root': str(self.root), 'data': str(self.data_root),
            'settings': str(self.settings_path), 'bootstrap_sha256': self.bootstrap_sha256,
            'trust_sha256': self.trust_sha256})).hexdigest()

    def validate_static(self):
        require(_path(self.root) == self.root and _path(self.data_root) == self.data_root,
                'exact_first_install_roots_required')
        component_origin(self.root, self.data_root, COMPONENT)
        require(_path(self.settings_path) == self.settings_path and self.settings_path != self.data_root
                and self.settings_path.is_relative_to(self.data_root), 'first_install_settings_scope_required')
        program = self.root / 'TagEncyclopedia.exe'
        guard(program)
        require(getattr(sys, 'frozen', False) and _path(Path(sys.executable)) == program,
                'actual_first_install_bootstrap_required')
        require(sha256_file(program) == self.bootstrap_sha256, 'first_install_bootstrap_changed')
        require(type(self.trust_bytes) is bytes and sha256(self.trust_bytes).hexdigest() == self.trust_sha256,
                'explicit_first_install_trust_required')


def compiled_enrollment_needed(context):
    """Read-only routing hint; original owner rechecks absence before authority."""
    require(type(context) is EncyclopediaEnrollmentContext, 'typed_enrollment_context_required')
    context.validate_static()
    base = context.root / 'State' / COMPONENT.value
    markers = [context.root / 'Apps' / COMPONENT.value,
        *(base / name for name in ('component-origin.json', 'active.json', 'transaction.json',
            'pending-transaction.json', 'connection-pending.json'))]
    for path in markers: guard(path)
    if any(path.exists() for path in markers):
        return False  # Corrupt/interrupted/existing state never becomes fresh install.
    store = ReaderConnectionStore(context.settings_path)
    return store.selected(store.observe()) is None


class StandaloneEncyclopediaEnrollment:
    """Retained precreation installer; explicit handoff keeps the original lease.

    An activated result still awaits an independently integrated original reader
    handoff and actual native health. The enrollment owner stays retained. A
    binding returned by enrolled_binding() is signed context, never a capability.
    """
    def __init__(self, context, acquisition, owner, *, channel=None):
        require(type(context) is EncyclopediaEnrollmentContext and type(owner) is DeploymentOwner,
                'original_enrollment_context_and_owner_required')
        context.validate_static()
        require(type(acquisition) is ComponentAcquisition and acquisition.trust == context.trust_bytes
                and acquisition.trust_pin == context.trust_sha256, 'original_enrollment_acquisition_required')
        require(acquisition.maximum <= MAX_ARCHIVE, 'staging_archive_bound_required')
        require(channel in (None, 'stable', 'beta'), 'explicit_enrollment_channel_required')
        zone = context.data_root / 'Encyclopedia/UpdateCache'
        require(acquisition.root != zone and acquisition.root.is_relative_to(zone), 'first_install_cache_scope_required')
        self.context, self.acquisition, self.owner = context, acquisition, owner
        self.channel = channel
        self.subject = owner.arm_standalone_enrollment(context)
        self.installation = EncyclopediaInstallation(acquisition, ReaderConnectionStore(context.settings_path),
            admission=ComponentStageAdmission(self._hold))
        self._reader_backend = None

    def _hold(self, component):
        return self.owner.standalone_enrollment_stage_hold(self.subject, component)

    def enrolled_binding(self):
        with self.owner.dispatch_lock:
            self.owner._standalone_enrollment_owned(self.subject)
            require('component_stage' not in self.owner.record, 'first_install_stage_still_owned')
            context = self.context
            binding = admit_standalone_binding(root=context.root, data_root=context.data_root,
                settings_path=context.settings_path, bootstrap_sha256=context.bootstrap_sha256,
                trust_bytes=context.trust_bytes, trust_sha256=context.trust_sha256)
            self.owner._standalone_enrollment_owned(self.subject)
            return binding

    def handoff_reader(self):
        """Explicit source API composition; never launches or forwards an entry."""
        with self.owner.dispatch_lock:
            if self._reader_backend is not None:
                self.owner._standalone_encyclopedia_owned(self._reader_backend.subject)
                return self._reader_backend
            binding = self.enrolled_binding()
            from suite_core.encyclopedia.bootstrap_entry import StandaloneEncyclopediaBootstrap
            self._reader_backend = StandaloneEncyclopediaBootstrap(binding, self.acquisition, self.owner,
                _enrollment_subject=self.subject)
            if self.channel is not None:
                self._reader_backend.update_check.channel = self.channel
            return self._reader_backend

    def close(self):
        """Close only original unhanded enrollment; reader lifecycle owns later Close."""
        require(self._reader_backend is None, 'original_reader_root_retained_after_enrollment_handoff')
        return self.owner.close_standalone_enrollment(self.subject)
