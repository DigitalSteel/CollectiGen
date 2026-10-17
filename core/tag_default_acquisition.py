"""Explicit stock acquisition evidence; no extraction, catalog Apply or feed activation.

The local a.0.1.2 delivery identifies TagDatabase in BOOTSTRAP_INVENTORY.
Its outer ZIP contains TagDatabase/data, not an executable component stage.
No published signed release/trust is supplied here. Hosts must inject admission.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from core.component_acquisition import ComponentAcquisition, ComponentOffer, AcquiredComponent
from suite_core.installation import ComponentId
from suite_core.discovery.component_release_signature import _version

TAG_DATABASE_COMPONENT = ComponentId("TagDatabase")


@dataclass(frozen=True)
class TagDefaultBaseline:
    catalog_id: str
    catalog_revision: str
    pack_id: str
    approved_release_fingerprint: str | None = None
    content_version: str | None = None
    observation_status: str = "unknown"
    distribution_version: str | None = None

    def __post_init__(self):
        for value in (self.catalog_id, self.catalog_revision, self.pack_id):
            if type(value) is not str or not value.strip() or len(value) > 512:
                raise ValueError("Explicit bounded native baseline identity required")
        if self.observation_status not in {"known", "verified_empty", "unknown", "recovery_pending"}:
            raise ValueError("Unsupported native baseline observation")
        if self.approved_release_fingerprint is not None and (
                type(self.approved_release_fingerprint) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", self.approved_release_fingerprint)):
            raise ValueError("Exact approved release fingerprint required")
        for value in (self.content_version, self.distribution_version):
            if value is not None:
                _version(value)
        if self.observation_status == "known" and any(value is None for value in (
                self.approved_release_fingerprint, self.content_version, self.distribution_version)):
            raise ValueError("Known defaults require approved content and distribution evidence")
        if self.observation_status == "verified_empty" and any(value is not None for value in (
                self.approved_release_fingerprint, self.content_version, self.distribution_version)):
            raise ValueError("Empty baseline cannot contain installed release evidence")


@dataclass(frozen=True)
class TagDefaultDiscovery:
    baseline: TagDefaultBaseline
    status: str
    offers: tuple
    reason: str
    checked_at_utc: str


@dataclass(frozen=True)
class AcquiredTagDefault:
    baseline: TagDefaultBaseline
    acquired: AcquiredComponent

    @property
    def production_install_authorized(self):
        return False


class TagDefaultAcquisition:
    """Single-worker facade over an explicitly supplied original stock service.

    component_id=None leaves acquisition unavailable. Supplying the exact ID is
    a host admission assertion, never authentication or publication clearance.
    Stock acquisition retains signature, hash, platform, trust and cache checks.
    """
    def __init__(self, *, acquisition=None, component_id=None):
        if acquisition is not None and type(acquisition) is not ComponentAcquisition:
            raise TypeError("Original stock ComponentAcquisition required")
        if component_id is not None and (type(component_id) is not ComponentId
                or component_id != TAG_DATABASE_COMPONENT):
            raise ValueError("Unsupported Tag Database component admission")
        self.acquisition = acquisition
        self.component_id = component_id
        self._discovery = None

    @staticmethod
    def _check(baseline, cancel):
        if type(baseline) is not TagDefaultBaseline:
            raise TypeError("Explicit native baseline observation required")
        if cancel is not None and cancel():
            raise InterruptedError("Tag data acquisition cancelled")

    def discover(self, *, baseline, channel, should_cancel=None):
        self._discovery = None
        self._check(baseline, should_cancel)
        if channel not in {"stable", "beta"}:
            raise ValueError("Explicit supported channel required")
        offers = ()
        status, reason = "unable_to_check", "Tag data acquisition is not admitted"
        if baseline.observation_status in {"unknown", "recovery_pending"}:
            reason = "Approved installed baseline or exact recovery is required"
        elif self.acquisition is not None and self.component_id is not None:
            try:
                versions = {} if baseline.observation_status == "verified_empty" else {
                    self.component_id.value: baseline.distribution_version}
                candidates = self.acquisition.discover(channel=channel,
                    installed_versions=versions, should_cancel=should_cancel, component_id=self.component_id)
                offers = tuple(row for row in candidates if self._offer_valid(row, baseline, channel))
                # Stock discover returns newer offers only, without signed
                # current-release evidence. An empty result cannot prove Current.
                status = "update_available" if offers else "unable_to_check"
                reason = "Verified compatible Tag data offer available" if offers else (
                    "No newer offer returned; verified current-release evidence is unavailable")
            except InterruptedError:
                raise
            except Exception as error:
                status, reason, offers = "unable_to_check", str(error), ()
        self._check(baseline, should_cancel)
        result = TagDefaultDiscovery(baseline, status, offers, reason,
            datetime.now(timezone.utc).isoformat())
        self._discovery = result
        return result

    def _offer_valid(self, offer, baseline, channel):
        if type(offer) is not ComponentOffer:
            raise TypeError("Stock verified offer required")
        if offer.release.component_id != self.component_id:
            return False
        if (offer.release.channel != channel or offer.release.os != self.acquisition.os
                or offer.release.architecture != self.acquisition.arch
                or offer.current_version != baseline.distribution_version
                or offer.trust_sha256 != self.acquisition.trust_pin):
            raise ValueError("Tag data offer differs from admitted baseline/platform/trust")
        return True

    def acquire(self, offer, *, baseline, confirmed=False, progress=None, should_cancel=None):
        self._check(baseline, should_cancel)
        discovery = self._discovery
        if (confirmed is not True or discovery is None or discovery.baseline != baseline
                or discovery.status != "update_available"
                or not any(row is offer for row in discovery.offers)):
            raise ValueError("Confirm the exact freshly discovered Tag data offer")
        self._discovery = None  # One-shot; every failed/cancelled transfer needs fresh review.
        self.acquisition.revalidate_offer(offer, should_cancel=should_cancel)
        acquired = self.acquisition.acquire(offer, progress=progress, should_cancel=should_cancel)
        self._check(baseline, should_cancel)
        if type(acquired) is not AcquiredComponent or acquired.offer is not offer:
            raise ValueError("Original acquired Tag data artifact required")
        return AcquiredTagDefault(baseline, acquired)
