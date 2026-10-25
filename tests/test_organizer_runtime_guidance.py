"""Contract failures preserve explicit runtime authority without repair or leaks."""
import json

import pytest

from suite_core.media_organizer.runtime_startup import RuntimeSelection, OrganizerRuntimeError


@pytest.fixture
def selection(tmp_path, monkeypatch):
    from suite_core.media_organizer import runtime_startup
    value = RuntimeSelection('organizer-image', tmp_path/'Runtime', tmp_path/'program')
    (value.program_root/'tools').mkdir(parents=True)
    (value.program_root/'tools/Repair-PortableRuntime.ps1').write_text('# synthetic tool; never executed')
    monkeypatch.setattr(runtime_startup.sys, 'executable', str(value.interpreter))
    monkeypatch.setattr(runtime_startup.subprocess, 'run', lambda *a, **k: pytest.fail('Unexpected repair execution'))
    return value


@pytest.mark.parametrize('failure', [FileNotFoundError, PermissionError, ValueError, TypeError])
def test_contract_read_failure_has_selected_guidance_without_raw_details(selection, monkeypatch, failure):
    import runtime_contract
    def unavailable(*args, **kwargs):
        raise failure('Synthetic raw diagnostic must remain hidden')
    monkeypatch.setattr(runtime_contract, 'load_expected_runtime_contract', unavailable)
    monkeypatch.setattr(runtime_contract, 'validate_runtime', lambda *a, **k: pytest.fail('Contract not admitted'))
    with pytest.raises(OrganizerRuntimeError) as caught:
        selection.validate()
    text = str(caught.value)
    assert 'unavailable or invalid' in text
    assert 'complete matching Organizer package' in text
    assert 'Selected profile: organizer-image' in text
    assert str(selection.runtime_root) in text
    assert 'Explicit repair:' in text and '-CapabilityProfile organizer-image' in text
    assert 'Synthetic raw diagnostic' not in text
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('defect', ['missing_pins', 'missing_lock', 'malformed_json', 'nested_shape', 'invalid_lock'])
def test_actual_contract_reader_failures_are_explained(selection, defect):
    program = selection.program_root
    (program/'tools').mkdir(parents=True, exist_ok=True)
    pins = {'schema_version': 2, 'python': {'version': '3.13.15', 'architecture': 'amd64'},
            'exiftool': {'version': '13.59'}}
    if defect == 'nested_shape': pins['python'] = []
    pins_path = program/'tools/runtime-pins.json'
    if defect != 'missing_pins':
        pins_path.write_text('{' if defect == 'malformed_json' else json.dumps(pins), encoding='utf-8')
    if defect != 'missing_lock':
        (program/'requirements-organizer-image-runtime.lock').write_text(
            'not-pinned\n' if defect == 'invalid_lock' else
            'Pillow==12.3.0\nPySide6_Essentials==6.11.2\nshiboken6==6.11.2\n', encoding='utf-8')
    with pytest.raises(OrganizerRuntimeError, match='Explicit repair:'):
        selection.validate()


def test_unexpected_programming_failure_is_not_relabelled(selection, monkeypatch):
    import runtime_contract
    def unexpected(*args, **kwargs): raise RuntimeError('Unexpected implementation failure')
    monkeypatch.setattr(runtime_contract, 'load_expected_runtime_contract', unexpected)
    with pytest.raises(RuntimeError, match='Unexpected implementation failure'):
        selection.validate()


def test_missing_repair_tools_are_honest_and_cannot_spawn(selection):
    (selection.program_root/'tools/Repair-PortableRuntime.ps1').unlink()
    text = selection.repair_guidance()
    assert 'Selected profile: organizer-image' in text
    assert 'repair tools are unavailable' in text
    assert 'Explicit repair:' not in text
    with pytest.raises(OrganizerRuntimeError, match='repair tools are unavailable'):
        selection.repair()
