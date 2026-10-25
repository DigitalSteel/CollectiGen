"""Synthetic data-integrity checks for the three-file Reader integration proposal."""
from pathlib import Path
import json
import pytest
from core.encyclopedia_catalog_creation import prepare_empty_catalog, confirm_empty_catalog
from core.tag_catalog_connection import TagConnectionHold


def inputs(tmp_path):
    settings=tmp_path/'settings.json'
    settings.write_bytes(b'{"unknown_future_setting":{"preserve":true}}')
    return settings, tmp_path/'new-catalog'


def test_runs_only_the_disposable_public_source():
    import core.encyclopedia_catalog_creation as creation
    assert Path(creation.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]


def test_review_has_no_writes_and_success_uses_native_validated_empty_catalog(tmp_path):
    settings, root=inputs(tmp_path)
    before=settings.read_bytes()
    review=prepare_empty_catalog(settings, root)
    assert not root.exists()
    assert confirm_empty_catalog(review, review.token)==root
    identity=json.loads((root/'data/user/catalog_identity.json').read_text())
    assert identity['catalog_id'] and identity['incarnation_id']
    assert (root/'data/user/tags.json').is_file()
    assert settings.read_bytes()==before
    assert not (root/'tag_catalog_snapshot.json').exists()
    with pytest.raises(ValueError):
        prepare_empty_catalog(settings, root)


@pytest.mark.parametrize('stop', ['wrong-token','cancel','changed-folder'])
def test_refusal_preserves_settings_and_preexisting_content(tmp_path, stop):
    settings, root=inputs(tmp_path)
    review=prepare_empty_catalog(settings, root)
    expected=TagConnectionHold
    token=review.token
    cancel=None
    if stop=='wrong-token': token='not-the-reviewed-token'
    elif stop=='cancel': cancel=lambda: True; expected=InterruptedError
    else:
        root.mkdir(); (root/'keep.bin').write_bytes(b'concurrent user content')
        expected=ValueError
    with pytest.raises(expected):
        confirm_empty_catalog(review, token, should_cancel=cancel)
    assert settings.read_bytes()==b'{"unknown_future_setting":{"preserve":true}}'
    assert not (root/'data').exists()
    if stop=='changed-folder': assert (root/'keep.bin').read_bytes()==b'concurrent user content'
    else: assert not root.exists()


def test_settings_folder_cannot_become_the_catalog(tmp_path):
    settings, _=inputs(tmp_path)
    with pytest.raises(TagConnectionHold):
        prepare_empty_catalog(settings, settings.parent)
