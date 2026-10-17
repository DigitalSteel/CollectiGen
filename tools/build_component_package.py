"""Package explicit frozen Windows Suite staging bytes; no freezing or signing."""
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from suite_core.installation import ComponentId, _relative
from suite_core.discovery.component_release_signature import _json, _version, MAX_METADATA_BYTES
from suite_core.discovery.component_staging import INVENTORY, MAX_ARCHIVE, MAX_UNPACKED, guard, canonical, require
from suite_core.discovery.offline_component_producer import produce_inventory

COMPONENTS = {'collectigen.suite-manager': 'manager', 'collectigen.tag-encyclopedia': 'encyclopedia',
              'collectigen.media-organizer': 'organizer'}
MUTABLE = {'data', 'models', 'catalogs', 'saves', 'logs', 'backups', '.git', '.venv', '__pycache__'}


def build_package(payload_root, output_root, *, runtime_manifest, module_manifest,
                  component_id, software_version, os_name, architecture, artifact_name,
                  supported_from, published_at, release_notes, require_update_data_contract=False):
    """Fail closed on mismatched inventory/identity; output is unsigned and inactive."""
    require(component_id in COMPONENTS, 'suite_component_required')
    require(type(require_update_data_contract) is bool, 'explicit_update_contract_requirement')
    require(not require_update_data_contract or component_id == 'collectigen.tag-encyclopedia',
            'encyclopedia_update_contract_required')
    require(os_name == 'windows' and architecture == 'x86_64', 'builder_windows_x64_only')
    version, minimum = _version(software_version), _version(supported_from)
    require(minimum.compare(version) <= 0, 'supported_from_newer_than_target')
    require(type(published_at) is str and 0 < len(published_at) <= 64
            and all(32 <= ord(c) < 127 for c in published_at), 'publication_date')
    require(type(release_notes) is str and bool(release_notes.strip()) and len(release_notes) <= 100000,
            'release_notes')
    require(type(artifact_name) is str and len(artifact_name) <= 255
            and len(_relative(artifact_name)) == 1 and artifact_name.endswith('.zip'), 'artifact_basename')
    _relative(runtime_manifest)
    _relative(module_manifest)
    source, output = Path(payload_root), Path(output_root)
    guard(source)
    guard(output)
    source, output = source.resolve(), output.resolve()
    guard(source)
    guard(output)
    require(source.is_dir() and not output.exists(), 'explicit_payload_and_fresh_output_required')
    require(not output.is_relative_to(source) and not source.is_relative_to(output), 'separate_output_required')
    members = []
    count = total = 0
    for path in source.rglob('*'):
        count += 1
        require(count <= 1024, 'payload_entry_bounds')
        guard(path)
        name = path.relative_to(source).as_posix()
        parts = _relative(name)
        require(not any(p.casefold() in MUTABLE for p in parts), 'mutable_or_generated_payload')
        require(not any(p.casefold() == INVENTORY.casefold() for p in parts), 'inventory_already_present')
        if path.is_dir():
            continue
        require(path.is_file(), 'regular_payload_only')
        total += path.stat().st_size
        require(total <= MAX_UNPACKED and len(members) < 512, 'payload_bounds')
        members.append((name, path.read_bytes()))
    members = tuple(sorted(members))
    by_name = dict(members)
    require(len(by_name) == len(members) and runtime_manifest in by_name and module_manifest in by_name,
            'required_manifests')
    runtime = _json(by_name[runtime_manifest], 'runtime manifest')
    module = _json(by_name[module_manifest], 'module manifest')
    require(type(runtime.get('schema_version')) is int and runtime['schema_version'] == 1
            and runtime.get('component') == COMPONENTS[component_id]
            and runtime.get('feature_version') == software_version, 'runtime_identity')
    require(runtime.get('os') == os_name and runtime.get('architecture') == architecture, 'runtime_platform')
    require(type(module.get('schema_version')) is int and module['schema_version'] == 1
            and module.get('module_id') == component_id and module.get('software_version') == software_version,
            'module_identity')
    require(runtime.get('inventory_excludes_itself') == 'RUNTIME_MANIFEST.json'
            and Path(runtime_manifest).name == 'RUNTIME_MANIFEST.json', 'runtime_self_exclusion')
    update_contract_validated = False
    if component_id == 'collectigen.tag-encyclopedia' and (
            require_update_data_contract or 'update_data_contract' in module):
        # Reuse the original updater's exact schema; never synthesize compatibility.
        # Its fixed installed/target module path must be inventoried verbatim.
        require(module_manifest == 'TagEncyclopedia/MODULE_MANIFEST.json',
                'signed_component_schema_manifest_path_required')
        from core.encyclopedia_update_binding import _data_contract
        compatibility = _data_contract(by_name[module_manifest], software_version)
        snapshots = compatibility.snapshot_interfaces
        declared = module.get('supported_snapshot_interface_versions')
        maximum = module.get('snapshot_interface_version')
        require(type(declared) is list and all(type(v) is int for v in declared)
                and tuple(sorted(declared)) == snapshots and type(maximum) is int
                and maximum == max(snapshots), 'update_snapshot_declaration_mismatch')
        update_contract_validated = True  # schema only, never installed-data health
    rows = runtime.get('files')
    require(type(rows) is list and 0 < len(rows) <= 512, 'runtime_file_rows')
    base = Path(runtime_manifest).parent.as_posix()
    expected = {}
    for row in rows:
        require(type(row) is dict and set(row) == {'path', 'size_bytes', 'sha256'}, 'runtime_row_schema')
        _relative(row['path'])
        name = row['path'] if base == '.' else base + '/' + row['path']
        require(name not in expected, 'duplicate_runtime_row')
        expected[name] = row
    require(set(by_name) == set(expected) | {runtime_manifest}, 'runtime_inventory_closure')
    for name, row in expected.items():
        require(type(row['size_bytes']) is int and row['size_bytes'] == len(by_name[name])
                and row['sha256'] == sha256(by_name[name]).hexdigest(), 'runtime_file_pin')
    inventory = produce_inventory(members, component_id=ComponentId(component_id),
        software_version=software_version, os_name=os_name, architecture=architecture)
    require(total + len(inventory) <= MAX_UNPACKED, 'final_payload_bounds')
    # Stale guard before creating any output. ZIP reads the admitted byte snapshot.
    require(all((source / n).is_file() and (source / n).read_bytes() == v for n, v in members), 'payload_changed')
    require({p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()} == set(by_name),
            'payload_closure_changed')
    output.mkdir()  # no force/overwrite, no deletion or alteration of input
    incomplete = output / 'INCOMPLETE.json'
    incomplete.write_bytes(canonical({'state': 'incomplete', 'production_install_authorized': False}))
    archive = output / artifact_name
    with archive.open('xb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, content in sorted((*members, (INVENTORY, inventory))):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content, compresslevel=9)
    require(archive.stat().st_size <= MAX_ARCHIVE, 'archive_size')
    with zipfile.ZipFile(archive) as z:
        require(z.namelist() == sorted((*by_name, INVENTORY)), 'output_closure')
        require(all(z.read(n) == v for n, v in (*members, (INVENTORY, inventory))), 'output_readback')
    envelope = canonical({'schema_version': 1, 'product': 'collectigen', 'component_id': component_id,
        'version': software_version, 'channel': 'beta' if version.prerelease else 'stable',
        'published_at': published_at, 'supported_from': supported_from, 'release_notes': release_notes,
        'platform': {'os': os_name, 'architecture': architecture},
        'artifacts': [{'name': artifact_name, 'kind': 'component-package', 'size_bytes': archive.stat().st_size,
            'sha256': sha256(archive.read_bytes()).hexdigest(), 'portable_manifest_sha256': sha256(inventory).hexdigest()}]})
    require(len(envelope) <= MAX_METADATA_BYTES, 'envelope_size')
    (output / 'release-manifest.input.json').write_bytes(envelope)
    (output / INVENTORY).write_bytes(inventory)
    receipt = {'schema_version': 1, 'component_id': component_id, 'software_version': software_version,
        'payload_files': len(members), 'archive_sha256': sha256(archive.read_bytes()).hexdigest(),
        'manifest_sha256': sha256(envelope).hexdigest(), 'inventory_sha256': sha256(inventory).hexdigest(),
        'production_install_authorized': False, 'native_health_verified': False, 'signed': False,
        'update_data_contract_schema_validated': update_contract_validated}
    (output / 'packaging-receipt.json').write_bytes(canonical(receipt))
    incomplete.unlink()  # only the exact marker just created in this fresh output
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('payload-root', 'output-root', 'runtime-manifest', 'module-manifest', 'component-id',
                 'software-version', 'os-name', 'architecture', 'artifact-name', 'supported-from',
                 'published-at', 'release-notes-file'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--require-update-data-contract', action='store_true',
        help='Require the existing Encyclopedia module compatibility schema; does not attest data compatibility.')
    values = vars(parser.parse_args())
    notes = Path(values.pop('release_notes_file'))
    values['release_notes'] = notes.read_text(encoding='utf-8')
    print(canonical(build_package(**values)).decode())


if __name__ == '__main__':
    main()
