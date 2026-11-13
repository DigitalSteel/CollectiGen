"""Stock installed SDK homes; no native process, network or user data."""
from hashlib import sha256
import pytest
from core.manager_successor_admission import installed_manager_payload_path
from core.manager_self_update_recovery import observe


@pytest.mark.parametrize(('member','installed'), [
    ('Squirrel.exe', 'Update.exe'),
    ('collectigen.suite-manager_ExecutionStub.exe', 'collectigen.suite-manager.exe'),
    ('CollectiGenManager.exe', 'current/CollectiGenManager.exe'),
    ('sq.version', 'current/sq.version'),
    ('core/library.pyd', 'current/core/library.pyd'),
])
def test_signed_members_resolve_to_stock_installed_bytes(tmp_path, member, installed):
    expected = tmp_path / 'Apps/Manager' / installed
    expected.parent.mkdir(parents=True, exist_ok=True)
    raw = ('signed fixture: ' + member).encode()
    expected.write_bytes(raw)
    actual = installed_manager_payload_path(tmp_path, member)
    assert actual == expected
    assert observe(actual) == dict(exists=True, size=len(raw), sha256=sha256(raw).hexdigest())
    expected.write_bytes(b'changed')
    assert observe(actual)['sha256'] != sha256(raw).hexdigest()
    expected.unlink()
    assert observe(actual) == dict(exists=False, size=0, sha256=None)


@pytest.mark.parametrize('member', ['', '../Update.exe', '/Update.exe', 'C:/Update.exe',
    'core/../../Update.exe', 'core\\Update.exe', './sq.version', 'core//library.pyd'])
def test_invalid_inventory_path_cannot_escape_fixed_homes(tmp_path, member):
    with pytest.raises(ValueError, match='installed_manager_payload_path'):
        installed_manager_payload_path(tmp_path, member)


def test_current_sdk_shadow_cannot_replace_real_installed_updater(tmp_path):
    shadow = tmp_path / 'Apps/Manager/current/Squirrel.exe'
    shadow.parent.mkdir(parents=True)
    shadow.write_bytes(b'authentic archive copy')
    actual = installed_manager_payload_path(tmp_path, 'Squirrel.exe')
    assert actual == shadow.parent.parent / 'Update.exe'
    assert observe(actual) == dict(exists=False, size=0, sha256=None)


def test_fresh_zip_stub_location_is_explicit_not_an_existence_fallback(tmp_path):
    member = 'collectigen.suite-manager_ExecutionStub.exe'
    initial = tmp_path / 'Apps/Manager/current' / member
    initial.parent.mkdir(parents=True)
    initial.write_bytes(b'signed SDK stub')
    assert installed_manager_payload_path(tmp_path, member, stock_applied=False) == initial
    applied = installed_manager_payload_path(tmp_path, member)
    assert applied == initial.parent.parent / 'collectigen.suite-manager.exe'
    assert observe(applied) == dict(exists=False, size=0, sha256=None)
    # A deleted post-apply stub never falls back to an old archive copy.
    applied.write_bytes(initial.read_bytes())
    assert installed_manager_payload_path(tmp_path, member, stock_applied=False) == initial
