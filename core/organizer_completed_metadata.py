"""Isolated owner-issued completed public metadata receipt; trust remains external."""
from hashlib import sha256
from pathlib import Path
import re
from suite_core.installation import ComponentId
from suite_core.discovery.component_release_signature import verify_component_release
from suite_core.discovery.component_staging import read_metadata_bytes
from suite_core.discovery.github_component_releases import release_asset_identity

COMPONENT=ComponentId('collectigen.media-organizer')
FIELDS={'schema','purpose','component_id','version','channel','platform','manifest','signature','trust_sha256','source','completed'}

def read_completed_inputs(directory,receipt,*,trust_bytes,root_trust_sha256,expected_version):
    """directory/receipt are exact original cache-owner selections, not a scan.

    Root owner must admit custody before calling. Receipt hashes are consistency,
    not trust; a receipt cannot supply a key or choose a trust file.
    """
    if type(trust_bytes) is not bytes or sha256(trust_bytes).hexdigest()!=root_trust_sha256:
        return None,'root_trust_unavailable'
    try:
        if type(receipt) is not dict or set(receipt)!=FIELDS or type(receipt['schema']) is not int or receipt['schema']!=1 or receipt['purpose']!='COMPLETED_PUBLIC_COMPONENT_METADATA' or receipt['component_id']!=COMPONENT.value or receipt['version']!=expected_version or receipt['channel']!='beta' or receipt['platform']!={'os':'windows','architecture':'x86_64'} or receipt['completed'] is not True or receipt['trust_sha256']!=root_trust_sha256:
            return None,'completed_metadata_receipt_unavailable'
        source=receipt['source']
        if type(source) is not dict or set(source)!={'manifest_url','signature_url'}:return None,'completed_metadata_receipt_unavailable'
        manifest_source=release_asset_identity(source['manifest_url']);signature_source=release_asset_identity(source['signature_url'])
        expected_tag=COMPONENT.value+'/v'+expected_version
        if manifest_source[0]!=expected_tag or signature_source[0]!=expected_tag or (manifest_source[1],signature_source[1]) not in {('release-manifest.json','release-manifest.json.sig'),('collectigen-release.json','collectigen-release.json.sig')}:return None,'completed_metadata_receipt_unavailable'
        raw=[]
        for field,name in [('manifest','collectigen-release.json'),('signature','collectigen-release.json.sig')]:
            row=receipt[field]
            if type(row) is not dict or set(row)!={'name','sha256','size_bytes'} or row['name']!=name or type(row['size_bytes']) is not int or not 0<row['size_bytes']<=128*1024 or type(row['sha256']) is not str or re.fullmatch('[0-9a-f]{64}',row['sha256']) is None:return None,'completed_metadata_receipt_unavailable'
            data=read_metadata_bytes(Path(directory)/name)
            if len(data)!=row['size_bytes'] or sha256(data).hexdigest()!=row['sha256']:return None,'completed_metadata_changed'
            raw.append(data)
        manifest,signature=raw
        verify_component_release(manifest,signature,trust_bytes,expected_component=COMPONENT,expected_version=expected_version,expected_channel='beta',expected_os='windows',expected_architecture='x86_64')
        return dict(manifest_bytes=manifest,signature_bytes=signature,trust_bytes=trust_bytes,expected_version=expected_version),'completed_metadata_verified'
    except FileNotFoundError:return None,'completed_metadata_missing'
    except OSError:return None,'completed_metadata_unreadable'
    except ValueError:return None,'completed_metadata_identity_unavailable'
