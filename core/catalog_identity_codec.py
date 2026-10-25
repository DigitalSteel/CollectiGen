"""Pure catalog identity codec and lifecycle intent; no session, enrollment or I/O."""
from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import UUID, uuid4

IDENTITY_SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 64 * 1024
_FIELDS = {"schema_version", "catalog_id", "incarnation_id"}


class CatalogIdentityHold(ValueError):
    pass


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise CatalogIdentityHold("A canonical nonzero catalog UUID is required") from None


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise CatalogIdentityHold("Duplicate catalog metadata fields")
        result[key] = value
    return result


def _json(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CatalogIdentityHold("Catalog metadata must be finite UTF-8 JSON") from None
    if len(raw) > MAX_METADATA_BYTES:
        raise CatalogIdentityHold("Catalog metadata exceeds its bound")
    return raw


@dataclass(frozen=True)
class CatalogIdentity:
    catalog_id: str
    incarnation_id: str
    extensions_json: str = "{}"

    def __post_init__(self):
        _uuid(self.catalog_id)
        _uuid(self.incarnation_id)
        try:
            extra = json.loads(self.extensions_json, object_pairs_hook=_pairs)
        except (ValueError, TypeError, RecursionError):
            raise CatalogIdentityHold("Invalid identity extensions") from None
        if not isinstance(extra, dict) or _FIELDS.intersection(extra):
            raise CatalogIdentityHold("Identity extensions cannot replace authority fields")
        object.__setattr__(self, "extensions_json", _json(extra).decode("utf-8"))

    @property
    def publication_catalog_id(self):
        # Existing Publication/CatalogBinding string field can retain the pair;
        # changing incarnation becomes an explicit select, never a refresh.
        return f"knowledge-catalog:{self.catalog_id}:{self.incarnation_id}"


def encode_identity(identity):
    if type(identity) is not CatalogIdentity:
        raise CatalogIdentityHold("An admitted catalog identity is required")
    payload = json.loads(identity.extensions_json)
    payload.update(schema_version=IDENTITY_SCHEMA_VERSION, catalog_id=identity.catalog_id,
                   incarnation_id=identity.incarnation_id)
    return _json(payload)


def decode_identity(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_METADATA_BYTES:
        raise CatalogIdentityHold("Explicit bounded catalog metadata bytes are required")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(CatalogIdentityHold("Nonfinite metadata")))
    except (ValueError, UnicodeError, RecursionError):
        raise CatalogIdentityHold("Invalid catalog identity metadata") from None
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != IDENTITY_SCHEMA_VERSION or not _FIELDS <= value.keys()):
        raise CatalogIdentityHold("Missing or unsupported catalog identity schema")
    return CatalogIdentity(value["catalog_id"], value["incarnation_id"],
                           _json({k: v for k, v in value.items() if k not in _FIELDS}).decode())


def prepare_legacy_enrollment(raw, *, decision):
    """Prospective bytes only, after explicit enrollment choice; never writes."""
    if raw is not None or decision != "enroll_selected_legacy_catalog":
        raise CatalogIdentityHold("Legacy enrollment requires absent metadata and an explicit decision")
    return CatalogIdentity(str(uuid4()), str(uuid4()))


def transition_identity(identity, *, operation):
    """Only named lifecycle intent can change identity; content is not inspected."""
    if type(identity) is not CatalogIdentity:
        raise CatalogIdentityHold("An admitted catalog identity is required")
    if operation in {"user_select", "relocate", "backup", "pack_import", "merge"}:
        return identity
    if operation == "independent_copy":
        return CatalogIdentity(str(uuid4()), str(uuid4()), identity.extensions_json)
    if operation == "full_backup_restore":
        return CatalogIdentity(identity.catalog_id, str(uuid4()), identity.extensions_json)
    raise CatalogIdentityHold("An explicit supported identity transition is required")


@dataclass(frozen=True)
class CatalogIdentityObservation:
    identity: CatalogIdentity
    metadata_sha256: str


def observe_identity(raw):
    return CatalogIdentityObservation(decode_identity(raw), sha256(raw).hexdigest())


def inverse_catalog_binding(raw):
    observed = observe_identity(raw)
    return {"schema_version": IDENTITY_SCHEMA_VERSION,
            "catalog_id": observed.identity.catalog_id,
            "incarnation_id": observed.identity.incarnation_id,
            "metadata_sha256": observed.metadata_sha256}


def require_inverse_catalog(raw, receipt_binding):
    if (type(receipt_binding) is not dict
            or set(receipt_binding) != {"schema_version", "catalog_id", "incarnation_id", "metadata_sha256"}
            or type(receipt_binding.get("schema_version")) is not int
            or receipt_binding["schema_version"] != IDENTITY_SCHEMA_VERSION):
        raise CatalogIdentityHold("Malformed inverse catalog binding")
    if receipt_binding != inverse_catalog_binding(raw):
        raise CatalogIdentityHold("Inverse belongs to a different or changed catalog incarnation")
