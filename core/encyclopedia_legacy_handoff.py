"""Selection preparation for the immutable 0.2.2 standalone entry contract.

No process creation, health assertion or lease transfer. The authenticated Root
caller owns the stage admission and retains this plan through ordinary Close.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from core.encyclopedia_connection import COMPONENT, ReaderConnectionStore
from core.encyclopedia_update_binding import _installed, admit_standalone_binding
from release_contract import sha256_file
from suite_core.discovery.component_staging import ComponentStageAdmission, require

LEGACY_VERSION = '0.2.2-alpha.1'
LEGACY_PROGRAM_SHA256 = '4f600bb9e18a500aa3bd9e95e598f3489f00399c0a946a72ba35345ffbca9e6a'
HOSTED_UPGRADE_VERSION = '0.3.1-alpha.1'
HOSTED_UPGRADE_PROGRAM_SHA256 = '5510bc0e3c93d56d22e92c60e31bd182ec4e209fbc59d5c91ef9a6a4a17b37a5'


class LegacyReaderRefusal(ValueError):
    """A checked validation refusal before this operation attempted a write."""


@dataclass(frozen=True)
class LegacyReaderHandoff:
    binding: object
    original_settings_path: Path
    original_settings_sha256: str
    prepared_settings_sha256: str

    def revalidate(self):
        current = self.binding.revalidate_initial()
        store = ReaderConnectionStore(self.original_settings_path)
        require(store.observe().digest == self.original_settings_sha256,
                'legacy_original_settings_changed')
        require(current.settings_sha256 == self.prepared_settings_sha256,
                'legacy_prepared_settings_changed')
        return current


def prepare_legacy_reader_handoff(admission, *, manager_settings_path,
                                  expected_selection, bootstrap_sha256,
                                  trust_bytes, trust_sha256, admitted_manager_settings_path=None):
    """Move only the connection into the fixed old Reader settings document.

    Signed active/history admission precedes publication. The stock settings
    writer preserves unknown fields and handles backup/readback/restoration.
    A failed publication or admission never creates a runnable handoff plan.
    """
    require(type(admission) is ComponentStageAdmission, 'original_legacy_stage_admission_required')
    require(sha256(trust_bytes).hexdigest() == trust_sha256, 'legacy_trust_pin_changed')
    with admission.transaction(COMPONENT) as (root, base, owner, check):
        source = Path(manager_settings_path)
        require(source.is_absolute() and source.is_relative_to(root / 'Data')
                and source.suffix.casefold() == '.json', 'legacy_original_settings_scope_required')
        try:
            initial = _installed(root, root / 'Data', source, trust_bytes)
            program_pin=sha256_file(initial.selected.program)
            legacy=(initial.selected.version==LEGACY_VERSION and program_pin==LEGACY_PROGRAM_SHA256)
            shared_upgrade=(initial.selected.version==HOSTED_UPGRADE_VERSION
                and program_pin==HOSTED_UPGRADE_PROGRAM_SHA256
                and source==root/'Data/Encyclopedia/settings.json'
                and (base/'legacy-reader-settings.json').exists()
                and admitted_manager_settings_path is not None)
            if shared_upgrade:
                from core.encyclopedia_connection import manager_reader_settings_path
                require(manager_reader_settings_path(root,admitted_manager_settings_path)==source,
                        'original_shared_reader_authority_required')
            require(initial.selected == expected_selection
                    and (legacy or shared_upgrade),
                    'exact_published_legacy_reader_required')
            target = root / 'Data/Encyclopedia/settings.json'
            store = ReaderConnectionStore(target)
            before = store.observe()
            require(store.selected(before) in (None, initial.selected),
                    'legacy_settings_selection_conflict')
        except (ValueError,OSError) as error:
            raise LegacyReaderRefusal(str(error)) from error
        check()
        # Revalidate the signed source and full settings digest before writing.
        try:
            require(_installed(root, root / 'Data', source, trust_bytes) == initial,
                    'legacy_source_selection_changed')
        except (ValueError,OSError) as error:
            raise LegacyReaderRefusal(str(error)) from error
        if store.selected(before) is None:
            store.save(initial.selected, expected=before)
        check()
        binding = admit_standalone_binding(root=root, data_root=root / 'Data',
            settings_path=target, bootstrap_sha256=bootstrap_sha256,
            trust_bytes=trust_bytes, trust_sha256=trust_sha256)
        require(binding.initial.selected == initial.selected,
                'legacy_published_selection_changed')
        original = ReaderConnectionStore(source).observe()
        # When source and target already coincide, the selection was unchanged.
        require(original.digest == initial.settings_sha256,
                'legacy_original_settings_changed')
        result = LegacyReaderHandoff(binding, source, original.digest,
                                    binding.initial.settings_sha256)
        result.revalidate()
        check()
        return result


def commit_legacy_selection_authority(admission, plan, manager_settings_path):
    """Choose the shared store only for the explicitly committed handoff."""
    from core.encyclopedia_connection import manager_reader_settings_path
    from suite_core.discovery.component_staging import atomic, read_json
    require(type(admission) is ComponentStageAdmission and type(plan) is LegacyReaderHandoff,
            'original_legacy_selection_commit_required')
    with admission.transaction(COMPONENT) as (root,base,owner,check):
        plan.revalidate()
        source=Path(manager_settings_path)
        if source.suffix.casefold()=='.ini':source=source.with_suffix('.json')
        require(source.is_absolute() and source.is_relative_to(root/'Data'),
                'legacy_original_settings_scope_required')
        target=root/'Data/Encyclopedia/settings.json'
        marker=base/'legacy-reader-settings.json'
        if marker.exists():
            require(manager_reader_settings_path(root,source)==target,
                    'legacy_selection_authority_changed')
            return None  # An earlier committed handoff already owns this choice.
        store=ReaderConnectionStore(source)
        historical=store.selected(store.observe())
        require(plan.original_settings_path==source and historical==plan.binding.initial.selected,
                'legacy_original_selection_changed')
        value=dict(schema=1,manager_settings_path=str(source),reader_settings_path=str(target),
                   prior_selection=historical.mapping())
        check();atomic(marker,value)
        require(read_json(marker)==value and manager_reader_settings_path(root,source)==target,
                'legacy_selection_authority_readback')
        check()
        return value


def cancel_legacy_selection_authority(admission, created):
    """Cancel only this request's exact new pointer; preserve both documents."""
    if created is None:return
    from suite_core.discovery.component_staging import read_json
    require(type(admission) is ComponentStageAdmission,'original_legacy_selection_cancel_required')
    with admission.transaction(COMPONENT) as (root,base,owner,check):
        marker=base/'legacy-reader-settings.json'
        require(read_json(marker)==created,'legacy_selection_cancel_changed')
        check();marker.unlink()
        require(not marker.exists(),'legacy_selection_cancel_readback')
        check()
