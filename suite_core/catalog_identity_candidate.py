"""Suite session binding with compatibility exports from the core identity codec."""
from dataclasses import dataclass

from core.catalog_identity_codec import (
    IDENTITY_SCHEMA_VERSION, MAX_METADATA_BYTES, CatalogIdentityHold,
    CatalogIdentity, CatalogIdentityObservation, encode_identity, decode_identity,
    prepare_legacy_enrollment, transition_identity, observe_identity,
    inverse_catalog_binding, require_inverse_catalog,
)
from suite_core.selected_catalog import CatalogBinding


@dataclass(frozen=True)
class IdentityBoundCatalog:
    observation: CatalogIdentityObservation
    session_binding: CatalogBinding


def bind_selected_catalog(raw, session_binding):
    observation = observe_identity(raw)
    if (type(session_binding) is not CatalogBinding
            or session_binding.catalog_id != observation.identity.publication_catalog_id):
        raise CatalogIdentityHold("Publication does not belong to this catalog incarnation")
    return IdentityBoundCatalog(observation, session_binding)


def accepts_catalog_binding(bound, *, current_metadata, current_session_binding):
    try:
        current = bind_selected_catalog(current_metadata, current_session_binding)
    except CatalogIdentityHold:
        return False
    return type(bound) is IdentityBoundCatalog and bound == current
