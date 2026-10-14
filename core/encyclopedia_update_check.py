"""Disconnected signed update notifications; no installation capability.

The host supplies fresh ownership observations from its existing original-root
owner. These DTOs do not authenticate an installation or create a second lease.
Manager-managed copies route back to Manager; standalone copies need a separately
qualified manual package review. No downloader, installer or background retry.
"""
from dataclasses import dataclass

from core.application_update import UpdateServiceError
from core.component_acquisition import ComponentAcquisition, ComponentOffer
from core.encyclopedia_connection import COMPONENT
from suite_core.discovery.component_staging import require
from suite_core.discovery.github_component_releases import _version


@dataclass(frozen=True)
class EncyclopediaUpdateObservation:
    version: str
    owner: str
    identity: str
    pending: bool = False

    def __post_init__(self):
        _version(self.version)
        require(self.owner in ('standalone', 'manager', 'unknown', 'conflict'),
                'explicit_update_owner_required')
        require(type(self.identity) is str and bool(self.identity), 'installation_identity_required')
        require(type(self.pending) is bool, 'pending_state_required')


@dataclass(frozen=True)
class EncyclopediaUpdateNotice:
    status: str
    reason: str
    route: str | None = None
    offer: ComponentOffer | None = None

    @property
    def production_install_authorized(self):
        return False


class EncyclopediaUpdateCheck:
    """Explicit synchronous check using the canonical signed acquisition service.

    Call from the existing retained worker, never the GUI thread. Failure clears
    prior notification state. Observations bind only a notification; executable
    replacement requires the existing original owner's fresh admission/recovery.
    """
    def __init__(self, acquisition, observe, *, channel):
        require(type(acquisition) is ComponentAcquisition, 'existing_acquisition_required')
        require(callable(observe), 'explicit_ownership_observer_required')
        require(channel in ('stable', 'beta'), 'explicit_channel_required')
        self.acquisition, self.observe, self.channel = acquisition, observe, channel
        self.notice = None

    def check(self, *, should_cancel=None):
        self.notice = None
        try:
            before = self.observe()
            require(type(before) is EncyclopediaUpdateObservation, 'exact_update_observation_required')
            require(before.owner in ('standalone', 'manager') and not before.pending,
                    'installation_ownership_or_recovery_held')
            offers = self.acquisition.discover(channel=self.channel,
                installed_versions={COMPONENT.value: before.version}, should_cancel=should_cancel)
            after = self.observe()
            require(type(after) is EncyclopediaUpdateObservation and after == before,
                    'installation_changed_during_check')
            selected = tuple(o for o in offers if o.release.component_id == COMPONENT)
            require(len(selected) <= 1, 'single_encyclopedia_offer_required')
            if selected:
                offer = selected[0]
                require(offer.current_version == before.version and offer.operation == 'update',
                        'exact_installed_update_required')
                result = EncyclopediaUpdateNotice('available', 'signed_update_available',
                    'manager' if before.owner == 'manager' else 'manual_package_review', offer)
            else:
                result = EncyclopediaUpdateNotice('not_found', 'no_newer_release_found_on_channel')
        except InterruptedError:
            raise
        except (OSError, UpdateServiceError):
            result = EncyclopediaUpdateNotice('unavailable', 'release_check_unavailable')
        except ValueError:
            result = EncyclopediaUpdateNotice('held', 'release_or_ownership_not_admitted')
        self.notice = result
        return result
