"""Explicit category reads and guarded local favorites/appearance onboarding."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
import json

from .model_resource_registry import ModelResourceError, _cancel
from .model_selection import ModelSelectionService
from .model_workspace import _pinned_model_file
from .swarm_model_import import PINNED_SWARM_REVISION, SwarmModelImport, _model_name, _SUBTYPES
from .swarm_onboarding_receipts import OnboardingReceipts, digest, validate_markers

MAX_FAVORITES = 256
MAX_SELECTED = 64
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ImportCategories:
    favorites: bool = False
    appearance: bool = False


@dataclass(frozen=True)
class ReadConsent:
    combined_user_data: bool = False
    user_settings: bool = False


@dataclass(frozen=True)
class SourceFavorite:
    subtype: str
    model_name: str
    held: str = ""


@dataclass(frozen=True)
class OnboardingSource:
    categories: ImportCategories
    consent: ReadConsent
    binding: dict
    account_fingerprint: str
    favorites: tuple[SourceFavorite, ...]
    theme_id: str
    theme: str
    appearance_issue: str
    source_digest: str


@dataclass(frozen=True)
class FavoriteTarget:
    source: SourceFavorite
    resource_id: str
    selected_path: str
    overwrite: bool = False


@dataclass(frozen=True)
class OnboardingOperation:
    category: str
    destination: str
    before: object
    after: object
    action: str
    marker: dict
    target: FavoriteTarget | None = None


@dataclass(frozen=True)
class OnboardingPreview:
    source: OnboardingSource
    favorites: tuple[FavoriteTarget, ...]
    appearance: bool
    overwrite_appearance: bool
    registry_token: str
    appearance_snapshot: dict
    inventory_fingerprint: str
    operations: tuple[OnboardingOperation, ...]


@dataclass(frozen=True)
class OnboardingOutcome:
    category: str
    destination: str
    status: str


@dataclass(frozen=True)
class OnboardingResult:
    receipt_id: str
    outcomes: tuple[OnboardingOutcome, ...]
    appearance: str = ""
    issue: str = ""


def _bounded_payload(payload):
    if not isinstance(payload, dict): raise ModelResourceError("SwarmUI returned an unsupported response.")
    try: size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ModelResourceError("SwarmUI returned an unsupported response.") from exc
    if size > MAX_RESPONSE_BYTES: raise ModelResourceError("SwarmUI response exceeds the supported bound.")


def _short(value, *, limit=1024):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def project_favorites(payload):
    raw = payload.get("starred_models")
    if not isinstance(raw, dict) or len(raw) > 32:
        raise ModelResourceError("SwarmUI favorites are unavailable or unsupported.")
    result = []
    for subtype, names in raw.items():
        if not _short(subtype, limit=80) or not isinstance(names, list) or len(names) > MAX_FAVORITES:
            raise ModelResourceError("SwarmUI favorites are malformed or exceed the supported bound.")
        for name in names:
            if not _short(name): raise ModelResourceError("SwarmUI favorite names are unsupported.")
            held = ""
            try: _model_name(name)
            except (ValueError, ModelResourceError): held = "Unsupported model file name"
            if subtype not in _SUBTYPES: held = "Unsupported model category"
            row = SourceFavorite(subtype, name, held)
            if row not in result: result.append(row)
            if len(result) > MAX_FAVORITES: raise ModelResourceError("Choose a smaller SwarmUI favorites set (maximum 256).")
    return tuple(sorted(result, key=lambda row: (row.subtype, row.model_name)))


def project_appearance(payload):
    settings, themes = payload.get("settings"), payload.get("themes")
    if not isinstance(settings, dict) or not isinstance(themes, dict):
        return "", "", "SwarmUI appearance settings are unavailable"
    field = settings.get("theme")
    if (not isinstance(field, dict) or field.get("type") not in ("text", "dropdown")
            or field.get("is_secret") is not False or not _short(field.get("value"), limit=160)):
        return "", "", "SwarmUI theme field is secret, missing or unsupported"
    identifier = field["value"]
    theme = themes.get(identifier)
    if not isinstance(theme, dict) or type(theme.get("is_dark")) is not bool:
        return identifier, "", "Selected SwarmUI theme has no qualified brightness"
    return identifier, "dark" if theme["is_dark"] else "light", ""


def _source_content(source):
    return {"categories": asdict(source.categories), "binding": source.binding,
        "account_fingerprint": source.account_fingerprint,
        "favorites": [asdict(row) for row in source.favorites],
        "theme_id": source.theme_id, "theme": source.theme, "appearance_issue": source.appearance_issue}


class SwarmOnboardingService:
    def __init__(self, workspace, paths, settings, *, settings_path, receipt_directory):
        self.workspace, self.paths, self.settings = workspace, paths, settings
        self.settings_path = Path(settings_path)
        self.receipts = OnboardingReceipts(receipt_directory)
        self.model_import = SwarmModelImport(workspace, paths)

    def read_categories(self, categories, consent, *, should_cancel=None):
        if (not isinstance(categories, ImportCategories) or not isinstance(consent, ReadConsent)
                or any(type(value) is not bool for value in (*asdict(categories).values(), *asdict(consent).values()))
                or not (categories.favorites or categories.appearance)):
            raise ModelResourceError("Choose a supported onboarding category.")
        if not consent.combined_user_data or categories.appearance and not consent.user_settings:
            raise ModelResourceError("Review and accept the disclosed SwarmUI data reads before continuing.")
        _cancel(should_cancel)
        record = self.paths._record()
        binding = self.paths._binding(record)
        if record.get("source_revision") != PINNED_SWARM_REVISION or binding.get("source_revision") != PINNED_SWARM_REVISION:
            raise ModelResourceError("This SwarmUI revision has no qualified onboarding importer.")
        try:
            transport, session = self.paths.manager._server_session(record)
            payload = transport.post("GetMyUserData", {"session_id": session}, max_bytes=MAX_RESPONSE_BYTES)
        except Exception as exc:
            raise ModelResourceError("The enrolled SwarmUI user data is unavailable; no preferences were imported.") from exc
        _bounded_payload(payload)
        principal = payload.get("user_name")
        if not _short(principal, limit=256):
            raise ModelResourceError("SwarmUI did not provide a qualified source account identity.")
        account = digest({"server_id": binding["server_id"], "endpoint": binding["endpoint"], "account": principal})
        favorites = project_favorites(payload) if categories.favorites else ()
        del payload, principal
        _cancel(should_cancel)
        theme_id = theme = issue = ""
        if categories.appearance:
            try: payload = transport.post("GetUserSettings", {"session_id": session}, max_bytes=MAX_RESPONSE_BYTES)
            except Exception as exc: raise ModelResourceError("SwarmUI appearance settings are unavailable.") from exc
            _bounded_payload(payload)
            theme_id, theme, issue = project_appearance(payload)
            del payload
        if self.paths._binding(self.paths._record()) != binding:
            raise ModelResourceError("SwarmUI enrollment changed during the read; review it again.")
        _cancel(should_cancel)
        source = OnboardingSource(categories, consent, deepcopy(binding), account, favorites, theme_id, theme, issue, "")
        return OnboardingSource(**{**source.__dict__, "source_digest": digest(_source_content(source))})

    def _validate_source(self, source):
        if not isinstance(source, OnboardingSource) or source.source_digest != digest(_source_content(source)):
            raise ModelResourceError("The onboarding source review changed; read it again.")

    def _verify_target(self, target, token, inventory, *, should_cancel=None, progress=None):
        if target.source.held: raise ModelResourceError(target.source.held)
        choices = self.model_import._choices(target.resource_id, target.selected_path,
            expected_registry=token, inventory=inventory)
        if (choices[0].subtype, choices[0].model_name) != (target.source.subtype, target.source.model_name):
            raise ModelResourceError("The chosen model file does not match this exact SwarmUI favorite.")
        resolution = self.workspace.registry.resolve(target.resource_id, location_path=target.selected_path,
            should_cancel=should_cancel, progress=progress)
        if resolution.status != "identity_verified":
            raise ModelResourceError("The chosen model file no longer matches its registered full-file identity.")

    @staticmethod
    def _operation(source, category, destination, before, after, action, target=None, history=()):
        source_digest = digest({"account": source.account_fingerprint, "revision": source.binding["source_revision"],
            "category": category, "value": asdict(target.source) if target else {"theme_id": source.theme_id, "theme": source.theme}})
        marker = {"schema_version": 1, "source_digest": source_digest, "account_fingerprint": source.account_fingerprint,
            "category": category, "destination": destination, "before": before,
            "after": after, "operation_id": digest({"source": source_digest, "destination": destination, "before": before, "after": after, "prior_imports": history})}
        return OnboardingOperation(category, destination, before, after, action, marker, target)

    def preview(self, source, *, favorites=(), appearance=False, overwrite_appearance=False, should_cancel=None, progress=None):
        self._validate_source(source)
        favorites = tuple(favorites)
        if len(favorites) > MAX_SELECTED or any(not isinstance(t, FavoriteTarget) for t in favorites):
            raise ModelResourceError("Choose at most 64 exact model files.")
        if (type(appearance) is not bool or type(overwrite_appearance) is not bool
                or favorites and not source.categories.favorites or appearance and not source.categories.appearance):
            raise ModelResourceError("Read the selected categories before reviewing imports.")
        if len({t.resource_id for t in favorites}) != len(favorites) or len({t.source for t in favorites}) != len(favorites):
            raise ModelResourceError("Choose each favorite and registered model only once.")
        snapshot, token = self.workspace.registry.snapshot()
        inventory = self.paths.read() if favorites else None
        if inventory is not None and inventory.binding != source.binding:
            raise ModelResourceError("SwarmUI folder enrollment changed; read the categories again.")
        operations = []
        for target in favorites:
            _cancel(should_cancel)
            if target.source not in source.favorites or type(target.overwrite) is not bool:
                raise ModelResourceError("Choose a favorite from the current source review.")
            self._verify_target(target, token, inventory, should_cancel=should_cancel, progress=progress)
            record = snapshot["resources"][target.resource_id]
            user = record.get("user", {})
            if not isinstance(user, dict) or "favorite" in user and type(user["favorite"]) is not bool:
                raise ModelResourceError("The local model preferences need review.")
            before = user.get("favorite")
            history = validate_markers(record.get("swarm_favorite_imports", []))
            action = "unchanged" if before is True else "keep_local" if before is False and not target.overwrite else "import"
            op = self._operation(source, "favorites", target.resource_id, before, True, action, target, history)
            if any(row["source_digest"] == op.marker["source_digest"] for row in history) and not target.overwrite:
                op = self._operation(source, "favorites", target.resource_id, before, True, "already_imported" if before is True else "keep_local", target, history)
            operations.append(op)
        settings = self.settings.appearance_import_snapshot(self.settings_path) if appearance else {}
        if appearance:
            if source.appearance_issue or not source.theme: raise ModelResourceError(source.appearance_issue or "Source appearance is unavailable.")
            if self.settings.ui_theme != settings["theme"]:
                raise ModelResourceError("Appearance changed in another editor; reload it before import.")
            before = settings["theme"]
            action = "unchanged" if before == source.theme else "import" if overwrite_appearance else "keep_local"
            operations.append(self._operation(source, "appearance", "ui_theme", before, source.theme, action, history=settings["imports"]))
        if not operations: raise ModelResourceError("Choose a model favorite or appearance to review.")
        if self.workspace.registry.snapshot()[1] != token:
            raise ModelResourceError("The model registry changed during review.")
        return OnboardingPreview(source, favorites, appearance, overwrite_appearance, token, settings,
            inventory.fingerprint if inventory else "", tuple(operations))

    def apply(self, preview, *, confirmed=False, should_cancel=None, progress=None):
        if not confirmed or not isinstance(preview, OnboardingPreview):
            raise ModelResourceError("Confirm the displayed onboarding review before importing.")
        fresh = self.read_categories(preview.source.categories, preview.source.consent, should_cancel=should_cancel)
        if fresh != preview.source: raise ModelResourceError("SwarmUI account or selected category data changed after review.")
        checked = self.preview(fresh, favorites=preview.favorites, appearance=preview.appearance,
            overwrite_appearance=preview.overwrite_appearance, should_cancel=should_cancel, progress=progress)
        if checked != preview: raise ModelResourceError("The source or destination changed after review.")
        pending = [op for op in preview.operations if op.action == "import"]
        receipt = self.receipts.create([{"marker": op.marker, "category": op.category,
            "destination": op.destination, "before_digest": digest(op.before)} for op in pending]) if pending else ""
        outcomes = []
        token = preview.registry_token
        appearance = issue = ""
        for op in preview.operations:
            if op.action != "import":
                outcomes.append(OnboardingOutcome(op.category, op.destination, op.action)); continue
            try:
                _cancel(should_cancel)
                current_source = self.read_categories(preview.source.categories, preview.source.consent, should_cancel=should_cancel)
                if current_source != preview.source: raise ModelResourceError("The source changed between onboarding steps.")
                if op.target is not None:
                    inventory = self.paths.read()
                    if inventory.fingerprint != preview.inventory_fingerprint or inventory.binding != preview.source.binding:
                        raise ModelResourceError("SwarmUI model folders changed after review.")
                    with _pinned_model_file(Path(op.target.selected_path), self.workspace.protected_roots):
                        self._verify_target(op.target, token, inventory, should_cancel=should_cancel, progress=progress)
                        if (self.read_categories(preview.source.categories, preview.source.consent, should_cancel=should_cancel) != preview.source
                                or self.paths.read() != inventory):
                            raise ModelResourceError("The source changed while verifying the selected model file.")
                        token = ModelSelectionService(self.workspace.registry).import_favorite(op.destination,
                            expected_registry=token, expected_favorite=op.before, marker=op.marker)
                else:
                    self.settings.import_appearance(op.after, expected=preview.appearance_snapshot,
                        marker=op.marker, path=self.settings_path)
                    appearance = op.after
                outcomes.append(OnboardingOutcome(op.category, op.destination, "completed"))
                self.receipts.complete(receipt, op.marker["operation_id"], "completed")
            except (ValueError, RuntimeError, OSError, InterruptedError) as exc:
                # Do not expose exception payloads from source adapters in saved receipts.
                issue = "Stopped safely; completed steps are retained. Review remaining steps again." if isinstance(exc, InterruptedError) else "A step could not finish. Reopen the receipt and review remaining steps; completed changes are retained."
                if not any(row.destination == op.destination and row.category == op.category for row in outcomes):
                    outcomes.append(OnboardingOutcome(op.category, op.destination, "held"))
                break
        done = {(row.category, row.destination) for row in outcomes}
        outcomes.extend(OnboardingOutcome(op.category, op.destination, "waiting") for op in preview.operations if (op.category, op.destination) not in done)
        return OnboardingResult(receipt, tuple(outcomes), appearance, issue)

    def review_receipts(self):
        """Reconcile destination markers, without reading Swarm or model files."""
        snapshot, _ = self.workspace.registry.snapshot()
        appearance = self.settings.appearance_import_snapshot(self.settings_path)
        result = []
        for identity, plan in self.receipts.recent():
            outcomes = []
            for step in plan["steps"]:
                marker = step["marker"]
                if step["category"] == "favorites":
                    record = snapshot["resources"].get(step["destination"], {})
                    history = validate_markers(record.get("swarm_favorite_imports", []))
                    current = record.get("user", {}).get("favorite")
                else:
                    history, current = appearance["imports"], appearance["theme"]
                prior = next((row for row in history if row["operation_id"] == marker["operation_id"]), None)
                status = "waiting" if prior is None else "completed" if prior == marker and current == marker["after"] else "local_correction_preserved" if prior == marker else "held"
                outcomes.append(OnboardingOutcome(step["category"], step["destination"], status))
            result.append(OnboardingResult(identity, tuple(outcomes)))
        return tuple(result)
