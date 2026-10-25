"""Borrowed in-process recipient scope, never transport or library discovery.

Only already-admitted settings/profile/library/binding metadata is observed.
No paths are opened, indexes queried, settings saved or scope selected here.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import sys
import os

from .organizer_encyclopedia_handoffs import HandoffHold, _text


@dataclass(frozen=True)
class HostedLibraryParticipant:
    library_id: str
    role: str
    share_safety_policy: str
    require_reviewed: bool
    root_token: str = field(repr=False)
    manager: object = field(repr=False, compare=False)

    def __post_init__(self):
        for value in (self.library_id, self.role, self.share_safety_policy, self.root_token):
            _text(value)
        if type(self.require_reviewed) is not bool or self.manager is None:
            raise HandoffHold("original_library_privacy_participant_required")


@dataclass(frozen=True)
class HostedRecipientScope:
    """Session-local profile token, exact ordered library IDs and original epoch.

    This is not a portable profile registry ID or permission to export Local Only.
    Borrowed owner references stay local and are never serialized into receipts.
    """
    profile_id: str
    library_ids: tuple[str, ...]
    participants: tuple[HostedLibraryParticipant, ...]
    scope_revision: int
    state: object = field(repr=False, compare=False)
    settings: object = field(repr=False, compare=False)
    dataset: object = field(repr=False, compare=False)
    binding: object = field(repr=False, compare=False)

    def __post_init__(self):
        _text(self.profile_id)
        if (type(self.library_ids) is not tuple or not 0 < len(self.library_ids) <= 64
                or len(set(self.library_ids)) != len(self.library_ids)):
            raise HandoffHold("bounded_exact_hosted_libraries_required")
        for value in self.library_ids:
            _text(value)
        if (type(self.participants) is not tuple or len(self.participants) != len(self.library_ids)
                or any(type(value) is not HostedLibraryParticipant for value in self.participants)
                or tuple(value.library_id for value in self.participants) != self.library_ids
                or type(self.scope_revision) is not int or self.scope_revision < 0
                or any(value is None for value in (self.state, self.settings, self.dataset, self.binding))):
            raise HandoffHold("exact_hosted_scope_observation_required")

    def matches(self, current):
        if type(current) is not HostedRecipientScope:
            return False
        current.__post_init__()
        return (self == current and self.state is current.state and self.settings is current.settings
                and self.dataset is current.dataset and self.binding is current.binding
                and all(a.manager is b.manager for a, b in zip(self.participants, current.participants)))


def require_hosted_scope(prepared, current):
    if type(prepared) is not HostedRecipientScope:
        raise HandoffHold("prepared_original_recipient_scope_required")
    prepared.__post_init__()
    if not prepared.matches(current):
        raise HandoffHold("recipient_profile_library_privacy_or_revision_changed")


class OrganizerHostedScopeReader:
    """Observe the original hosted binding's epoch; no new event/cache owner.

    Constructor reads nothing. Invocation uses the exact admitted native host,
    already-loaded profile constants and configured included-library metadata.
    No filesystem, live index, image or sidecar operation occurs.
    """
    def __init__(self, host, *, profile_id_reader=None):
        self.host = host
        self.profile_id_reader = profile_id_reader or self._loaded_profile_id

    @staticmethod
    def _loaded_profile_id():
        paths = sys.modules.get("core.paths")
        root = getattr(paths, "USER_DATA_DIR", None)
        if root is None:
            raise HandoffHold("original_loaded_profile_identity_required")
        # Opaque local binding token, not a newly minted persistent authority.
        return sha256(str(root).encode("utf-8")).hexdigest()

    def __call__(self):
        from suite_core.encyclopedia.hosted_examples import HostedLibraryBinding
        from .library_profiles import LibraryProfile
        host = self.host
        host.organizer._check_context()
        state, dataset = host.state, host.state.dataset
        entry = getattr(host, "_hosted_encyclopedia_entry", None)
        binding = getattr(entry, "binding", None)
        if (type(binding) is not HostedLibraryBinding or binding.session is not state
                or binding.dataset is not dataset or binding._closed or binding._closing
                or getattr(entry, "browser", None) is not binding.window
                or getattr(binding.window, "_hosted_binding", None) is not binding):
            raise HandoffHold("original_hosted_recipient_binding_required")
        with binding._lock:
            epoch = binding._epoch
        ids = tuple(dataset.scope_library_ids)
        participants = []
        if not 0 < len(ids) <= 64:
            raise HandoffHold("bounded_exact_hosted_libraries_required")
        for library_id in ids:
            profile, manager = dataset.profile(library_id), dataset.manager(library_id)
            if (type(profile) is not LibraryProfile or profile.stable_id != library_id
                    or profile.recognized_role is None or manager is None):
                raise HandoffHold("original_library_privacy_participant_required")
            root=getattr(manager,"root",None)
            if (root is None or os.path.normcase(os.path.normpath(str(root)))
                    != os.path.normcase(os.path.normpath(profile.root))):
                raise HandoffHold("library_root_binding_changed")
            participants.append(HostedLibraryParticipant(library_id, profile.role,
                profile.share_safety_policy, profile.require_reviewed_image_knowledge,
                sha256(profile.root.encode("utf-8")).hexdigest(), manager))
        scope = HostedRecipientScope(self.profile_id_reader(), ids, tuple(participants), epoch,
                                    state, host.settings, dataset, binding)
        with binding._lock:
            if epoch != binding._epoch or tuple(dataset.scope_library_ids) != ids:
                raise HandoffHold("hosted_scope_changed_during_observation")
        host.organizer._check_context()
        return scope
