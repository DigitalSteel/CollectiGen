"""Reviewed empty native database creation over stock staging; no source/import/settings changes."""
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
import json
from core.application_profile import empty_profile_stamp
from core.tag_catalog_connection import _initialize, _path, TagConnectionHold

@dataclass(frozen=True)
class EmptyCatalogReview:
    root: Path
    settings_path: Path
    stamp: tuple
    token: str

def prepare_empty_catalog(settings_path, root):
    settings, root = _path(settings_path), _path(root)
    if settings == root or root in settings.parents:
        raise TagConnectionHold('Keep your database separate from app settings.')
    stamp = empty_profile_stamp(root)
    token = sha256(json.dumps({'root': str(root), 'settings': str(settings), 'stamp': stamp}, sort_keys=True).encode()).hexdigest()
    return EmptyCatalogReview(root, settings, stamp, token)

def confirm_empty_catalog(review, token, *, should_cancel=None):
    if type(review) is not EmptyCatalogReview or token != review.token:
        raise TagConnectionHold('Confirm the exact empty-database review.')
    if prepare_empty_catalog(review.settings_path, review.root) != review:
        raise TagConnectionHold('The folder changed. Choose an empty folder and review it again.')
    if should_cancel is not None and should_cancel():
        raise InterruptedError('Database creation cancelled before any catalog write.')
    # Stock sibling stage, native serializers/identity/capture, stale guards and publish.
    # Once this indivisible publish starts, settlement retains new data even if Stop is requested.
    _initialize(review.root, review.stamp)
    return review.root
