"""Candidate-only admission for one existing Encyclopedia native read route.

No production qualification, format allocation, writer or lock implementation.
Source evidence permits testing this exact source route; it is not a capability
claim made by a catalog, a completed receipt, or packaged-client qualification.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from .catalog_identity_codec import CatalogIdentity, decode_identity, MAX_METADATA_BYTES
from . import tag_catalog_connection as connection

ENTRY = "core.encyclopedia_catalog.EncyclopediaCatalog.open_catalog"
RECIPE = "NativeTagCatalogAdapter.capture/prepare_effective_tag_snapshot_reader"
# Partial dependency pins for this candidate experiment, NOT a complete
# interpretation chain or authentication of the executable being run.
# Production qualification must bind the full artifact or a complete chain.
SOURCE_PATHS = (
    "core/encyclopedia_read_admission.py", "core/encyclopedia_catalog.py",
    "suite_core/knowledge/native_tag_catalog_host.py",
    "core/effective_tag_snapshot_reader.py", "core/catalog_identity_codec.py",
    "core/catalog_admission.py", "core/knowledge_pack.py",
    "core/default_sync.py", "core/default_catalog_release.py",
    "core/tag_catalog_connection.py", "core/automation_rule_conversion.py",
    "core/knowledge_write_admission.py", "core/history_versions.py",
)
MAX_TRACKING_BYTES = 8 * 1024 * 1024  # Conservative candidate bound; larger stores hold.
_STATE_FIELDS = frozenset({"schema_version", "records", "tag_lifecycle",
    "input_fingerprint", "last_sync", "tag_appearance_default_hash", "approved_pack_releases"})
_DECLARED_SEMANTICS = frozenset({"retained_default_compatibility", "required_read_semantics",
    "required_semantics", "read_requirements", "required_capabilities", "store_contract",
    "semantic_requirements", "capabilities"})


def _hold(message):
    raise connection.TagConnectionHold(message)


def check_cancel(should_cancel):
    if should_cancel is not None and should_cancel():
        raise InterruptedError("Selected catalog read cancelled; previous view retained")


def _digest(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _immutable_stamp(value):
    return type(value) in (str, int) or (type(value) is tuple and all(_immutable_stamp(v) for v in value))


@dataclass(frozen=True)
class CandidateReadEvidence:
    """Partial source dependency pins; no client/complete-chain authentication."""
    source_pins: tuple
    entry: str = ENTRY
    recipe: str = RECIPE

    def __post_init__(self):
        if (type(self.source_pins) is not tuple or len(self.source_pins) != len(SOURCE_PATHS)
                or any(type(row) is not tuple or len(row) != 2
                    or type(row[0]) is not str or type(row[1]) is not str
                    or len(row[1]) != 64 or any(c not in "0123456789abcdef" for c in row[1])
                    for row in self.source_pins)
                or tuple(row[0] for row in self.source_pins) != SOURCE_PATHS
                or type(self.entry) is not str or type(self.recipe) is not str
                or self.entry != ENTRY or self.recipe != RECIPE):
            _hold("Exact candidate source/entry/read recipe evidence required")


def validate_candidate_evidence(evidence):
    if type(evidence) is not CandidateReadEvidence:
        _hold("Typed candidate source evidence required; no production read qualification")
    evidence.__post_init__()
    source_root = Path(__file__).resolve().parent.parent
    for relative, expected in evidence.source_pins:
        if sha256((source_root / relative).read_bytes()).hexdigest() != expected:
            _hold("Candidate client/read route source changed; review exact source evidence again")


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            _hold("Ambiguous duplicate read-requirement metadata")
        result[key] = value
    return result


def _bounded_metadata(path, maximum, *, optional=False):
    connection._path(path)
    if optional and not path.exists():
        return None
    if not path.is_file():
        _hold("Required enrolled catalog metadata is absent")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not raw or len(raw) > maximum:
        _hold("Read-requirement metadata exceeds its bounded candidate envelope")
    return raw


def _check_tracking_envelope(value):
    # Inspect metadata keys, not inline record meaning. Never turn an unknown
    # declaration hidden in a known carrier into a legacy/native recipe.
    pending = [(value, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 262144:
            _hold("Tracking metadata exceeds bounded semantic preflight")
        if type(node) is dict:
            if _DECLARED_SEMANTICS.intersection(node):
                _hold("Unqualified declared read semantics in tracking metadata")
            pending.extend((child, depth + 1) for child in node.values()
                if type(child) in (dict, list))
        elif type(node) is list:
            pending.extend((child, depth + 1) for child in node if type(child) in (dict, list))
    for key in ("records", "tag_lifecycle", "approved_pack_releases"):
        if key in value and type(value[key]) is not dict:
            _hold("Ambiguous tracking metadata carrier")


@dataclass(frozen=True)
class SelectedReadObservation:
    root: Path
    directory_pins: tuple
    identity: CatalogIdentity
    header_pin: str
    tracking_pin: str

    def __post_init__(self):
        if (type(self.root) is not type(Path(".")) or not self.root.is_absolute()
                or type(self.identity) is not CatalogIdentity
                or type(self.directory_pins) is not tuple or len(self.directory_pins) != 4
                or any(type(row) is not tuple or len(row) != 3
                    or row[0] != relative or type(row[1]) is not int or type(row[2]) is not int
                    for row, relative in zip(self.directory_pins, (".", "data", "data/user", "data/defaults")))
                or not _digest(self.header_pin) or not _digest(self.tracking_pin)):
            _hold("Immutable exact selected read observation required")


def observe_selected_read(root, evidence, *, should_cancel=None):
    """Bounded path/header/inline-metadata preflight, before record hashing."""
    check_cancel(should_cancel)
    validate_candidate_evidence(evidence)
    root = connection._path(root)
    pins = []
    for relative in (".", "data", "data/user", "data/defaults"):
        path = connection._path(root / relative)
        if not path.is_dir():
            _hold("Selected enrolled catalog directory is absent")
        info = path.stat()
        pins.append((relative, info.st_dev, info.st_ino))
    header = _bounded_metadata(root / "data/user/catalog_identity.json", MAX_METADATA_BYTES)
    identity = decode_identity(header)
    # There is no qualified advanced header contract in this candidate batch.
    if identity.extensions_json != "{}":
        _hold("Unknown enrolled header semantics require separate read qualification")
    tracking = _bounded_metadata(root / "data/user/default_knowledge_state.json",
        MAX_TRACKING_BYTES, optional=True)
    if tracking is not None:
        try:
            value = json.loads(tracking, object_pairs_hook=_pairs,
                parse_constant=lambda _: _hold("Nonfinite read-requirement metadata"))
        except (ValueError, UnicodeError, RecursionError):
            _hold("Malformed read-requirement metadata")
        if type(value) is not dict:
            _hold("Ambiguous read-requirement metadata envelope")
        if value.get("retained_default_compatibility"):
            _hold("Retained default semantics are not qualified for ordinary candidate Open")
        if set(value) - _STATE_FIELDS:
            _hold("Unknown read-requirement metadata; do not interpret as legacy")
        if value and (type(value.get("schema_version")) is not int or value["schema_version"] != 6):
            _hold("Unsupported default tracking schema")
        _check_tracking_envelope(value)
    check_cancel(should_cancel)
    return SelectedReadObservation(root, tuple(pins), identity, sha256(header).hexdigest(),
        sha256(b"absent" if tracking is None else b"present\0" + tracking).hexdigest())


@dataclass(frozen=True)
class ReadAdmissionBinding:
    """Detached immutable comparison evidence; never an edit/import token."""
    observation: SelectedReadObservation
    evidence: CandidateReadEvidence
    stamp: tuple
    catalog_id: str
    snapshot_fingerprint: str
    generation: int

    def __post_init__(self):
        if (type(self.observation) is not SelectedReadObservation
                or type(self.evidence) is not CandidateReadEvidence
                or type(self.stamp) is not tuple or not _immutable_stamp(self.stamp)
                or len(self.stamp) != 6 or self.stamp[:2] != self.observation.directory_pins[2][1:]
                or self.catalog_id != self.observation.identity.publication_catalog_id
                or not _digest(self.snapshot_fingerprint)
                or type(self.generation) is not int or self.generation < 1):
            _hold("Deeply immutable exact read binding required")
        self.observation.__post_init__()
        self.evidence.__post_init__()

    @property
    def candidate_only(self):
        return True


def validate_read_binding(binding, *, root, identity, publication, stamp):
    """Pure correspondence check; host must additionally own the exact staged view."""
    from suite_core.encyclopedia.publication import Publication
    if (type(binding) is not ReadAdmissionBinding
            or type(binding.observation) is not SelectedReadObservation
            or type(binding.evidence) is not CandidateReadEvidence
            or type(publication) is not Publication
            or binding.observation.root != root or binding.observation.identity != identity
            or binding.stamp != stamp or binding.catalog_id != publication.catalog_id
            or binding.catalog_id != identity.publication_catalog_id
            or binding.snapshot_fingerprint != publication.snapshot.fingerprint
            or binding.generation != publication.generation):
        _hold("Read-only delivery differs from exact core admission binding")
    binding.__post_init__()
    return binding
