"""Deliberate standalone optional-content registration over the shared core factory."""
from pathlib import Path
from core.tag_database_feed_binding import factory_for_tag_data
from core.tag_database_feed_registration import published_tag_data_registration
from suite_core.discovery.component_staging import guard, require
from suite_core.discovery.component_release_signature import MAX_METADATA_BYTES
from suite_core.installation import _path


def standalone_tag_data_factory(*, settings_path, trust_bytes=None, trust_path=None):
    """Return a lazy user-action factory; construction has no cache/catalog/network I/O."""
    settings = Path(settings_path)
    require(settings.is_absolute(), 'original_standalone_settings_required')
    require((trust_bytes is None) != (trust_path is None), 'one_existing_public_trust_source_required')
    if trust_path is not None:
        trust_path = Path(trust_path)
        require(trust_path.is_absolute(), 'explicit_public_trust_path_required')
    used = False

    def opt_in(host):
        nonlocal used
        require(not used, 'retain_original_standalone_tag_data_owner')
        require(Path(host.service.settings_path) == settings
            and Path(host.controller.settings_path) == settings,
            'original_standalone_tag_data_settings_binding_required')
        public_trust = trust_bytes
        if trust_path is not None:
            guard(trust_path)
            require(trust_path.is_file() and trust_path.stat().st_size <= MAX_METADATA_BYTES,
                'bounded_existing_public_trust_required')
            public_trust = trust_path.read_bytes()
        registration = published_tag_data_registration(public_trust)
        parent = settings.parent / 'TagDataAcquisition'
        guard(parent)
        selected = host.tag_data_native_view() if callable(getattr(host, 'tag_data_native_view', None)) else None
        if selected is not None:
            require(not parent.is_relative_to(selected.adapter.root)
                and not selected.adapter.root.is_relative_to(parent), 'data_cache_catalog_overlap')
        if not parent.exists():
            guard(parent.parent)
            if not parent.parent.exists():
                # Only the original app settings/cache ancestry; no catalog enrollment.
                parent.parent.mkdir(parents=True, exist_ok=False)
            require(parent.parent.is_dir(), 'original_settings_parent_required')
            parent.mkdir(exist_ok=False)
        require(_path(parent) == parent and parent.is_dir(), 'original_tag_data_cache_parent_required')
        from .version import SOFTWARE_VERSION
        factory = factory_for_tag_data(registration, opt_in=True,
            cache_parent=parent, host_version=SOFTWARE_VERSION)
        used = True
        return factory(host)

    return opt_in
