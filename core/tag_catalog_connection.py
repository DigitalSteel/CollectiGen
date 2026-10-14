"""Explicit native Tag catalog connection; no global profile binding or migration.

Local source pins compare the selected reviewed content, not signature or
acquisition authority. Native import/recovery and derived publication remain
owned by the existing adapter and transaction service.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from . import knowledge_pack as native
from .application_profile import empty_profile_stamp, publish_empty_profile
from .settings import AppSettings, _settings_writer
from .training_source_io import _assert_unredirected
from suite_core.catalog_identity_candidate import CatalogIdentity, decode_identity, encode_identity
from suite_core.discovery.component_staging import verify_directory, read_metadata_bytes
from suite_core.installation import ComponentId
from suite_core.encyclopedia.publication import read
from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter, bind_foundation_source_release
from suite_core.knowledge.tag_transactions import TagCatalogTransactions
from suite_core.selected_catalog import SelectedCatalogSession
from suite_core.database_actions import CatalogRelease
from suite_core.knowledge.catalog_migration_preview import PackRevision
from suite_core.manager.tag_database_controller import TagDatabaseChoice, _observe, empty_default_layer
from suite_core.knowledge import Snapshot, Source


class TagConnectionHold(RuntimeError):
    """Actionable connection failure; existing evidence remains authoritative."""


def _path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise TagConnectionHold("Select an exact absolute folder or settings path.")
    _assert_unredirected(path)
    return path


def _settings(path):
    from .history_configuration import MAX_SETTINGS_BYTES
    _path(path)
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(MAX_SETTINGS_BYTES + 1)
    if len(raw) > MAX_SETTINGS_BYTES:
        raise TagConnectionHold("Settings exceed the supported bound; resolve settings recovery first.")
    AppSettings._from_bytes(raw)
    return raw


def _source(directory):
    directory = _path(directory)
    inventory = directory / "BOOTSTRAP_INVENTORY.json"
    native._check_export_path(inventory)
    raw = read_metadata_bytes(inventory)
    profile = native._foundation_json(raw)
    pin = sha256(raw).hexdigest()
    version = profile["software_version"]
    verify_directory(directory, pin, ComponentId("TagDatabase"), version)
    pack = directory / "TagDatabase.zip"
    decoded = native.load_foundation_pack(pack)
    release = bind_foundation_source_release(decoded, read(directory / "metadata/TagDatabase.source-snapshot.json").snapshot)
    verify_directory(directory, pin, ComponentId("TagDatabase"), version)
    return decoded, release, (version, pin, sha256(pack.read_bytes()).hexdigest(), release.fingerprint)


def _catalog_stamp(root):
    """Pin native participants, exact default membership and managed receipts."""
    _path(root)
    from .catalog_admission import CatalogAdmissionState
    state = CatalogAdmissionState(root, native)
    if not state.enrolled:
        raise TagConnectionHold("Select an enrolled catalog. Existing legacy data needs explicit native migration review.")
    defaults = root / "data/defaults"
    _path(defaults)
    members = tuple(sorted(p.name for p in defaults.iterdir() if p.suffix == ".json"))
    paths = set(native._pack_authority_paths(root))
    paths.update(defaults / name for name in members)
    paths.add(root / "data/user/catalog_identity.json")
    paths.add(root / "tag_catalog_snapshot.json")
    pins = tuple(sorted((p.relative_to(root).as_posix(), native._file_fingerprint(p)) for p in paths))
    user = root / "data/user"
    info = user.stat()
    return (info.st_dev, info.st_ino, tuple(sorted(state.binding.items())), state.receipt_epoch, members, pins)


@dataclass(frozen=True)
class TagConnectionReview:
    settings_path: Path
    source_directory: Path
    catalog_root: Path
    create_new: bool
    settings_before: bytes | None
    source_pin: tuple
    catalog_stamp: tuple
    token: str


@dataclass(frozen=True)
class TagConnectionResult:
    """Immutable connection evidence around the existing Manager choice.

    Pass ``choice`` to the existing controller. Its transactions retain the
    native host and selected session; receipts remain explicitly selectable.
    source_pin is (component version, inventory SHA, archive SHA, release SHA).
    """
    choice: TagDatabaseChoice
    source_directory: Path
    source_pin: tuple
    catalog_identity: CatalogIdentity


def _token(values):
    return sha256(repr(values).encode("utf-8")).hexdigest()


def prepare_connection(settings_path, source_directory, catalog_root, create_new):
    """Read-only review. No catalog, settings, publication or lock is created."""
    if type(create_new) is not bool:
        raise TypeError("Explicit create_new decision required.")
    settings, source, root = map(_path, (settings_path, source_directory, catalog_root))
    if (root == source or root in source.parents or source in root.parents
            or settings == root or root in settings.parents
            or settings == source or source in settings.parents):
        raise TagConnectionHold("Keep catalog data separate from the source component and Manager settings.")
    before = _settings(settings)
    if before is not None:
        saved = json.loads(before).get("tag_database_connection", {})
        if type(saved) is not dict or type(saved.get("schema_version", 1)) is not int or saved.get("schema_version", 1) != 1:
            raise TagConnectionHold("Saved connection schema needs explicit review before reconnecting.")
    _decoded, _available, pin = _source(source)
    stamp = empty_profile_stamp(root) if create_new else _catalog_stamp(root)
    values = (settings, source, root, create_new, before, pin, stamp)
    return TagConnectionReview(*values, _token(values))


def _initialize(root, expected):
    """Stock empty native serializers in a sibling stage; enroll before publish."""
    from .tag_database import TagDatabase
    from .category_registry import CategoryRegistry
    from .relationship_store import RelationshipStore
    from .character_projection_storage import (character_record_storage_payloads,
        SERIES_STORAGE_SCHEMA_VERSION, CHARACTER_STORAGE_SCHEMA_VERSION, OUTFIT_STORAGE_SCHEMA_VERSION)
    stage = Path(tempfile.mkdtemp(prefix=".tag-catalog-stage-", dir=root.parent))
    user, defaults = stage / "data/user", stage / "data/defaults"
    user.mkdir(parents=True)
    defaults.mkdir()
    payloads = {"tags.json": TagDatabase(user / "tags.json")._payload(),
        "categories.json": CategoryRegistry(defaults / "categories.json", user / "categories.json").user_payload(),
        "relationships.json": RelationshipStore(user / "relationships.json").payload()}
    for kind, key, schema in (("series", "series", SERIES_STORAGE_SCHEMA_VERSION),
            ("character", "characters", CHARACTER_STORAGE_SCHEMA_VERSION), ("outfit", "outfits", OUTFIT_STORAGE_SCHEMA_VERSION)):
        payloads[key + ".json"] = {"schema_version": schema,
            key: character_record_storage_payloads(kind, (), (), ()), "disabled_default_ids": []}
    for name, value in payloads.items():
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        path = user / name
        path.write_bytes(raw)
        if path.read_bytes() != raw:
            raise TagConnectionHold("Catalog stage read-back failed; retain the sibling stage.")
    identity = CatalogIdentity(str(uuid4()), str(uuid4()))
    raw = encode_identity(identity)
    (user / "catalog_identity.json").write_bytes(raw)
    if decode_identity((user / "catalog_identity.json").read_bytes()) != identity:
        raise TagConnectionHold("Catalog identity read-back failed; retain the stage.")
    staged_adapter = NativeTagCatalogAdapter(stage, native=native,
        catalog_id=identity.publication_catalog_id, source_reference="Explicitly selected native Tag catalog")
    staged_adapter.capture(1)  # Prove stock reader/admission before publishing data.
    publish_empty_profile(root, stage, expected=expected)
    return identity


def _choice(root, source, decoded, available, *, publish_initial=False):
    identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
    adapter = NativeTagCatalogAdapter(root, native=native, catalog_id=identity.publication_catalog_id,
        source_reference="Explicitly selected native Tag catalog", foundation_proposal=decoded.proposal)
    adapter._state()
    from .catalog_admission import _receipts
    pending = tuple(row for row in _receipts(root, native) if row[1]["status"] == "pending")
    destination = root / "tag_catalog_snapshot.json"
    native._check_export_path(destination)
    if pending:
        if not destination.is_file():
            raise TagConnectionHold("Recovery is pending and the saved reader snapshot is missing; retain receipts for native recovery review.")
        publication = read(destination)
        baseline = publication.snapshot
    else:
        previous = read(destination) if destination.exists() else None
        if previous is not None and previous.catalog_id != identity.publication_catalog_id:
            raise TagConnectionHold("Saved reader belongs to a different catalog incarnation; retain it for review.")
        current = adapter.capture(previous.generation if previous else 1)
        # The confirmed reload advances the real saved publication. Seeding its
        # session with current native data at the old saved generation would
        # manufacture a generation change without a new authority revision.
        publication = previous if publish_initial and previous is not None else current
        baseline = current.snapshot
    if publication.catalog_id != identity.publication_catalog_id:
        raise TagConnectionHold("Saved reader belongs to a different catalog incarnation; retain it for review.")
    session = SelectedCatalogSession(publication)
    service = TagCatalogTransactions(adapter.host(), session, destination=destination)
    if empty_default_layer(adapter):
        # This is the observed absent-default baseline, never a prior version
        # of personal records. The live session retains the actual full snapshot.
        observed = baseline.source
        baseline = Snapshot(Source("Observed empty default layer", observed.revision,
            observed.authority_fingerprint, (), kind=observed.kind), (), (), ())
    base = CatalogRelease(PackRevision(available.revision.pack_id, "0.0.0", baseline),
        available.released_on, ("Selected native baseline before first default installation",), "Selected native catalog")
    choice = _observe(TagDatabaseChoice("Selected Tag catalog", service, base, available,
        source / "TagDatabase.zip", False, native_adapter=adapter, approved_releases=(available,)))
    if choice.observation_status == "unknown":
        raise TagConnectionHold("Installed release lacks its approved source snapshot; select its native owner review before updating.")
    if publish_initial and not pending:
        service.reload()
    return choice


def confirm_connection(review, confirmed_token):
    """Revalidate review, initialize only confirmed empty/new data, save pins."""
    if type(review) is not TagConnectionReview or confirmed_token != review.token:
        raise TagConnectionHold("Confirm the exact connection review token.")
    fresh = prepare_connection(review.settings_path, review.source_directory, review.catalog_root, review.create_new)
    if fresh != review:
        raise TagConnectionHold("Connection changed after review; review the current source and catalog again.")
    decoded, available, pin = _source(review.source_directory)
    if pin != review.source_pin:
        raise TagConnectionHold("Source changed after review; review the selected source again before connecting.")
    if _settings(review.settings_path) != review.settings_before:
        raise TagConnectionHold("Manager settings changed after review; reopen connection review.")
    if review.create_new:
        _initialize(review.catalog_root, review.catalog_stamp)
    elif _catalog_stamp(review.catalog_root) != review.catalog_stamp:
        raise TagConnectionHold("Catalog changed before confirmation; review again.")
    choice = _choice(review.catalog_root, review.source_directory, decoded, available, publish_initial=True)
    identity = decode_identity((review.catalog_root / "data/user/catalog_identity.json").read_bytes())
    if _source(review.source_directory)[2] != review.source_pin:
        raise TagConnectionHold("Source changed during connection. Catalog data is retained; review the source again.")
    payload = json.loads(review.settings_before) if review.settings_before else {}
    connection = dict(payload.get("tag_database_connection", {}))
    connection.update(schema_version=1, source_directory=str(review.source_directory), component="TagDatabase",
        version=pin[0], inventory_sha256=pin[1], archive_sha256=pin[2], release_fingerprint=pin[3],
        catalog_root=str(review.catalog_root), catalog_id=identity.catalog_id, incarnation_id=identity.incarnation_id)
    payload["tag_database_connection"] = connection
    settings = AppSettings._from_bytes(review.settings_before) if review.settings_before else AppSettings()
    # Catalog initialization is a separately durable operation. A racing settings
    # writer holds persistence, retaining the enrolled data for explicit reconnect.
    with _settings_writer(review.settings_path):
        if _settings(review.settings_path) != review.settings_before:
            raise TagConnectionHold("Manager settings changed during connection. Catalog data is retained; select the enrolled folder again.")
        try:
            settings._save_locked(review.settings_path, _patch_payload=payload)
        except Exception as error:
            raise TagConnectionHold("Connection settings could not be saved. Catalog data is retained; select this enrolled folder again. " + str(error)) from error
        if json.loads(_settings(review.settings_path))["tag_database_connection"] != connection:
            raise TagConnectionHold("Connection settings read-back differs; retain catalog and settings recovery evidence.")
        return TagConnectionResult(choice, review.source_directory, pin, identity)


def open_saved_connection(settings_path):
    """Reopen exact pinned enrolled roots; never enroll, migrate or pick a receipt."""
    settings = _path(settings_path)
    raw = _settings(settings)
    if raw is None:
        raise TagConnectionHold("Choose a Tag source and catalog folder first.")
    record = json.loads(raw).get("tag_database_connection")
    if (type(record) is not dict or type(record.get("schema_version")) is not int
            or record.get("schema_version") != 1 or record.get("component") != "TagDatabase"):
        raise TagConnectionHold("Choose or review the saved Tag connection schema first.")
    fields = ("source_directory", "catalog_root", "version", "inventory_sha256",
        "archive_sha256", "release_fingerprint", "catalog_id", "incarnation_id")
    if any(type(record.get(key)) is not str or not record[key].strip() for key in fields):
        raise TagConnectionHold("Saved Tag connection is incomplete. Explicitly choose and review its source and catalog again.")
    source, root = _path(record["source_directory"]), _path(record["catalog_root"])
    decoded, available, pin = _source(source)
    if pin != tuple(record.get(k) for k in ("version", "inventory_sha256", "archive_sha256", "release_fingerprint")):
        raise TagConnectionHold("Saved source changed. Explicitly review a source selection again.")
    _catalog_stamp(root)
    identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
    if (identity.catalog_id, identity.incarnation_id) != (record.get("catalog_id"), record.get("incarnation_id")):
        raise TagConnectionHold("Saved catalog identity changed. Explicitly select and review its incarnation again.")
    choice = _choice(root, source, decoded, available)
    if _settings(settings) != raw or _source(source)[2] != pin:
        raise TagConnectionHold("Saved connection changed while opening; reopen it.")
    return TagConnectionResult(choice, source, pin, identity)
