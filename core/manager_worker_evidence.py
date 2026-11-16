"""Shared read-only verification of an explicitly delegated signed Manager worker.

This function issues no session, execution grant, updater, store or lease. Callers
must hold their original typed admission and provide its actual Manager witness.
"""
from hashlib import sha256
from pathlib import Path
import time

from suite_core.discovery.component_staging import guard, require, MAX_METADATA_BYTES
from suite_core.discovery.manager_runtime_detached_admission import (
    PROFILE, MANAGER_MAX_ARCHIVE, manager_inventory_json, read_manager_inventory_proof,
    admit_installed_detached,
)
from suite_core.manager.bootstrap_entry import ProcessBinding, check_process, check_training_baseline_offer


def verify_retained_manager_worker(*, root, reference, expected_version, expected_channel,
                                   trust_bytes, trust_sha256, manager_identity, check):
    """Re-admit the exact original Recovery reference; absence stays unavailable."""
    require(callable(check), 'original_worker_admission_check_required')
    check()
    require(type(manager_identity) is ProcessBinding, 'actual_manager_worker_witness_required')
    check_process(manager_identity)
    require(type(trust_bytes) is bytes and sha256(trust_bytes).hexdigest() == trust_sha256,
            'worker_compiled_publisher_trust_changed')
    if reference is None:
        return None
    root = Path(root)
    require(root.is_absolute() and '..' not in root.parts, 'original_worker_code_root_required')
    guard(root)
    profile = dict(current_version=expected_version, channel=expected_channel, trust_sha256=trust_sha256)
    release = check_training_baseline_offer(reference, profile, manager_identity)
    for spec in release['files'].values():
        relative = Path(spec['path'])
        require(not relative.is_absolute() and '..' not in relative.parts and ':' not in str(relative),
                'compiled_retained_relative_path_required')
        path = root / relative
        require(path.is_relative_to(root / 'Recovery'), 'compiled_prior_recovery_zone_required')
        guard(path)
        spec['path'] = str(path)
    files = release['files']
    proof = read_manager_inventory_proof(files['proof'])
    def metadata(name):
        check()
        spec = files[name]
        require(type(spec['size']) is int and 0 < spec['size'] <= MAX_METADATA_BYTES,
                'training_proof_metadata_bounds')
        path = Path(spec['path'])
        guard(path)
        with path.open('rb') as stream:
            value = stream.read(spec['size'] + 1)
        require(len(value) == spec['size'] and sha256(value).hexdigest() == spec['sha256'],
                'training_proof_metadata_changed')
        return value
    archive = Path(files['archive']['path'])
    require(type(files['archive']['size']) is int and 0 < files['archive']['size'] <= MANAGER_MAX_ARCHIVE
            and archive.is_file() and archive.stat().st_size == files['archive']['size'],
            'training_proof_archive_reference_changed')
    manifest, signature, trust = metadata('manifest'), metadata('signature'), metadata('trust')
    require(trust == trust_bytes, 'worker_compiled_publisher_trust_changed')
    next_peer_check = 0.0
    def should_cancel():
        nonlocal next_peer_check
        # Archive verification invokes this for every small read. Native witness
        # checks hash retained executable images, so bound that repeated work;
        # this phase only reads software and the final check is unconditional.
        if time.monotonic() >= next_peer_check:
            check()
            next_peer_check = time.monotonic() + 0.25
        return False
    plan = admit_installed_detached(archive, inventory_profile=PROFILE,
        detached_inventory_bytes=proof, manifest_bytes=manifest, signature_bytes=signature, trust_bytes=trust,
        expected_trust_sha256=trust_sha256, expected_inventory_sha256=files['inventory']['sha256'],
        expected_version=expected_version, expected_channel=expected_channel,
        installed_executable_sha256=manager_identity.executable_sha256, should_cancel=should_cancel)
    require(plan.artifact_sha256 == files['archive']['sha256']
            and manager_inventory_json(proof)['archive_size_bytes'] == files['archive']['size'],
            'training_proof_archive_reference_changed')
    check()
    return plan, proof
