"""Reviewed public Tag-data target; caller supplies existing trust, no I/O."""
from core.component_acquisition import ExactComponentOffer, ExactOfferAsset
from core.tag_database_feed_binding import TagDataFeedRegistration
from core.tag_default_acquisition import TAG_DATABASE_COMPONENT
from suite_core.installation import InstalledComponent, InstallationId, ComponentKind, CapabilityId, InterfaceBinding, LocalInventory

TRUST_SHA256 = 'ef58994610d2684cbfbb97ddb29b171d27e4d76aa51339a11a0964367a97b826'
# Transport evidence must come from an original stock execution receipt.
# Original persisted r3 transfer uses the unchanged stock policy.
# Configured redirect limit is not an observed hop-count claim.
TRANSPORT_EVIDENCE_SHA256 = '917c224b9cb2f72ef0f9160a3402fecce96f644a18a3be15def04d0429b2e6c3'
TRANSPORT_QUALIFIED = True
ASSET_REDIRECT_ORIGINS = ('https://release-assets.githubusercontent.com',
    'https://objects.githubusercontent.com', 'https://github-releases.githubusercontent.com')
REQUIRED_ASSETS = {'collectigen-release.json': {'browser_download_url': 'https://github.com/DigitalSteel/CollectiGen/releases/download/TagDatabase/v0.2.1-alpha.1/collectigen-release.json', 'digest': 'sha256:ad001af8c4ca32d8ca9c6ad8db283762e889312a024ea02e57a206990d88d66f', 'id': 640764308, 'name': 'collectigen-release.json', 'size': 2409}, 'collectigen-release.json.sig': {'browser_download_url': 'https://github.com/DigitalSteel/CollectiGen/releases/download/TagDatabase/v0.2.1-alpha.1/collectigen-release.json.sig', 'digest': 'sha256:e20ad9d7472f3f67c7c7f157e2138718a4a06ea3934ae28a617df6f2808b048b', 'id': 640764268, 'name': 'collectigen-release.json.sig', 'size': 731}, 'TagDatabase-0.2.1-alpha.1.zip': {'browser_download_url': 'https://github.com/DigitalSteel/CollectiGen/releases/download/TagDatabase/v0.2.1-alpha.1/TagDatabase-0.2.1-alpha.1.zip', 'digest': 'sha256:9c361e0e99d1680d28b357cfc142cd65eae60f0e46a86beb8e3dbefc3458fd97', 'id': 640764487, 'name': 'TagDatabase-0.2.1-alpha.1.zip', 'size': 5902937}}


def published_tag_data_registration(trust_bytes):
    if not TRANSPORT_QUALIFIED:
        raise ValueError('stock_transport_execution_qualification_pending')
    def asset(name):
        row = REQUIRED_ASSETS[name]
        return ExactOfferAsset(name, row['size'], row['digest'].removeprefix('sha256:'),
            row['browser_download_url'], row['id'])
    expected = ExactComponentOffer(TAG_DATABASE_COMPONENT, '0.2.1-alpha.1', 'beta',
        'windows', 'x86_64', 'tag-manager-release-2026', TRUST_SHA256,
        1301951104, 412865338, 'TagDatabase/v0.2.1-alpha.1',
        asset('collectigen-release.json'), asset('collectigen-release.json.sig'),
        asset('TagDatabase-0.2.1-alpha.1.zip'))
    component = InstalledComponent(InstallationId('tag-data-feed-a021'), TAG_DATABASE_COMPONENT,
        expected.software_version, ComponentKind.APP, frozenset({CapabilityId('tag.defaults')}),
        (InterfaceBinding('snapshot_interface_version', 1),),
        LocalInventory('Apps/TagDatabase/data', 'BOOTSTRAP_INVENTORY.json',
            'b0161c89b26e1d11609340c191c00e8b4d31ee5e2b0c12f0d8e9606d817ead6f'))
    return TagDataFeedRegistration('defaultTagDatabase', trust_bytes, TRUST_SHA256,
        expected, component, ASSET_REDIRECT_ORIGINS, TRANSPORT_EVIDENCE_SHA256)
