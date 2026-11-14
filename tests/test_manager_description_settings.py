"""Manager caption settings use native persistence with generated configuration."""
import json
from pathlib import Path
from hashlib import sha256
import pytest

from core.manager_description_settings import ManagerDescriptionSettings, DescriptionSettingsError
from core.settings import FLORENCE2_MODEL_ID


def test_missing_explicit_settings_are_not_written_until_save(tmp_path):
    path = tmp_path / "Manager/generation-settings.json"
    store = ManagerDescriptionSettings(path); observed = store.observe()
    assert observed.digest is None and not path.exists()
    assert observed.values["ai_models_directory"] == str(path.parent / "models")
    saved = store.save({"plain_language_model": FLORENCE2_MODEL_ID}, expected=observed)
    assert saved.values["plain_language_model"] == FLORENCE2_MODEL_ID
    assert saved.digest == sha256(path.read_bytes()).hexdigest()
    assert not (path.parent / "models").exists(), "Saving settings must not install or inspect models"


def test_unknown_fields_and_existing_managed_authority_remain_unchanged(tmp_path):
    path = tmp_path / "generation-settings.json"
    managed = {"schema":1,"binding":"a"*64,"suite_id":"generated","revision":1,
        "suite_fingerprint":"b"*64,"detached":False,"receipts":{},
        "groups":{"appearance.theme":{"mode":"inherited","value_fingerprint":"c"*64}}}
    raw = json.dumps({"future":{"keep":[1,2]},"ui_theme":"light","suite_managed_settings":managed}).encode()
    path.write_bytes(raw)
    store = ManagerDescriptionSettings(path)
    store.save({"ai_models_directory": str(tmp_path / "shared-models"),"joycaption_do_sample":True},expected=store.observe())
    value = json.loads(path.read_bytes())
    assert value["future"] == {"keep":[1,2]} and value["suite_managed_settings"] == managed
    assert value["ui_theme"] == "light"
    assert next((tmp_path / "settings-history").glob("*.json")).read_bytes() == raw


def test_stale_caption_editor_cannot_overwrite_other_settings(tmp_path):
    path = tmp_path / "generation-settings.json"; path.write_text('{"future":1}',encoding="utf-8")
    store = ManagerDescriptionSettings(path); observed = store.observe()
    replacement = b'{"future":2}'; path.write_bytes(replacement)
    with pytest.raises(DescriptionSettingsError,match="settings_changed"):
        store.save({"joycaption_num_beams":2},expected=observed)
    assert path.read_bytes() == replacement


def test_native_readback_failure_restores_prior_bytes(tmp_path,monkeypatch):
    path = tmp_path / "generation-settings.json"; path.write_bytes(b'{"future":[1,2]}'); raw = path.read_bytes()
    store = ManagerDescriptionSettings(path); observed = store.observe(); stock = Path.read_text
    def corrupt(self,*args,**kwargs):
        return "{}" if self == path else stock(self,*args,**kwargs)
    monkeypatch.setattr(Path,"read_text",corrupt)
    with pytest.raises(DescriptionSettingsError,match="description_save_failed"):
        store.save({"joycaption_num_beams":2},expected=observed)
    assert path.read_bytes() == raw
    assert next((tmp_path / "settings-history").glob("*.json")).read_bytes() == raw


@pytest.mark.parametrize("changes",[
    {"ai_models_directory":"relative/models"},{"joycaption_num_beams":True},
    {"joycaption_min_new_tokens":1000,"joycaption_max_new_tokens":256},
    {"ui_theme":"dark"},
])
def test_rejected_choices_do_not_create_or_change_configuration(tmp_path,changes):
    path = tmp_path / "settings.json"; store = ManagerDescriptionSettings(path)
    with pytest.raises(DescriptionSettingsError):store.save(changes,expected=store.observe())
    assert not path.exists()
