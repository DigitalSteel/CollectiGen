"""Exact published Organizer offer for the default Manager installer."""
from core.component_acquisition import ExactComponentOffer, ExactOfferAsset
from suite_core.installation import ComponentId
from suite_core.manager.organizer_acquisition_binding import CompiledOrganizerAcquisitionProfile

PROFILE = CompiledOrganizerAcquisitionProfile(
    trust_bytes=b'{\n  "schema_version": 1,\n  "keys": [\n    {\n      "key_id": "tag-manager-release-2026",\n      "algorithm": "rsa-pkcs1v15-sha256",\n      "status": "active",\n      "modulus": "7X-kvNmNEcSUC57gcMwSYYtvVeoaal5sNaU4diS1kE7iMS3s8kV3C10v5eDvPwqoMyxr6Fat6AQTaL6MuQ3bg3-cy6Ny_2x5IHeIzbK2wwmHDWcrZDGdrsKNkU1nYv92CFoyQwjo-pWo3xjTlnk0o5MjeaVazc9biVDoB8i22nUx89T_loR5bgox2GLvP1M81FiebeR46u0yIKGOeqcOHw_XdecmtWqBYYtGHFbCjVuH4U44EyU0lFYeI6dxcpAcVAQM6SG12t1EJgYp3RQeX770jFK2uoz-GRF3Al11wjX4ttXRl-s6NHDcKvJOEbb61YLBfomdG3cBUNk63PCqekXBAA412OacC7Fq3qnq0uU4Yd9_zfdeGldb_YbuESoGEyaVK3bvQ67wQrFdCU2Rm54a5XWPwX7BPClpOiG2_4ORfo3Kflpr9SjMBzVqzA1C9Cc4XM2OYs2ejExXUBY98WL9i1VV0lqf8SQAskTacfo_Pa3gVxFubMqu_zN7pbUF",\n      "exponent": 65537\n    }\n  ]\n}\n',
    trust_sha256='ef58994610d2684cbfbb97ddb29b171d27e4d76aa51339a11a0964367a97b826',
    asset_redirect_origins=('https://release-assets.githubusercontent.com',),
    transport_qualification_sha256='14518fac0111875bf1a4f97cdf67ff2db642d68d5c8c2d9c4d7d03a91140d9e4',
    expected_offer=ExactComponentOffer(
        component_id=ComponentId('collectigen.media-organizer'),
        software_version='0.4.23-alpha.1',
        channel='beta',
        os='windows',
        architecture='x86_64',
        key_id='tag-manager-release-2026',
        trust_sha256='ef58994610d2684cbfbb97ddb29b171d27e4d76aa51339a11a0964367a97b826',
        repository_id=1301951104,
        release_id=434057072,
        tag='collectigen.media-organizer/v0.4.23-alpha.1',
        repository='DigitalSteel/CollectiGen',
        manifest=ExactOfferAsset(
            name='release-manifest.json',
            size_bytes=1874,
            sha256='cf26960230bfd8b0a977367400b03280c95c505143d600b3d16deb9d7c6066a2',
            url='https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.4.23-alpha.1/release-manifest.json',
            asset_id=709859342,
        ),
        signature=ExactOfferAsset(
            name='release-manifest.json.sig',
            size_bytes=731,
            sha256='6e9a043b3d47f943a22be8c41802c31b20c1a21917b9111cf333b461d48b3e4a',
            url='https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.4.23-alpha.1/release-manifest.json.sig',
            asset_id=709859382,
        ),
        artifact=ExactOfferAsset(
            name='MediaOrganizer-a.0.4.23-windows-x64.zip',
            size_bytes=117724240,
            sha256='aad65a14a2b40008d9a6597250eb15b823fd2df0fd78700f2300edc27db48802',
            url='https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.4.23-alpha.1/MediaOrganizer-a.0.4.23-windows-x64.zip',
            asset_id=709858531,
        ),
    ),
)
