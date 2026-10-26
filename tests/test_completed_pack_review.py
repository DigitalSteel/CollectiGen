"""Generated native receipts only; read-only review never enables an inverse."""
from dataclasses import FrozenInstanceError
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest
import core.knowledge_pack as native
from core.catalog_identity_codec import CatalogIdentity, encode_identity, inverse_catalog_binding


@pytest.fixture
def completed(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    helper_path = Path(__file__).with_name('test_knowledge_pack_records.py')
    spec = importlib.util.spec_from_file_location('completed_review_helpers', helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    source, root = helper._root(tmp_path / 'source'), helper._root(tmp_path / 'recipient')
    helper._write(source / 'data/user/tags.json', {'schema_version': 10, 'tags': [
        {'id': 'review_added', 'display': 'Review addition', 'category': 'colors'}]})
    header = root / 'data/user/catalog_identity.json'
    header.write_bytes(encode_identity(CatalogIdentity(
        '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002')))
    pack = native.export_knowledge_pack(source, source / 'ordinary-user.zip')
    plan = native.plan_knowledge_pack_import(root, pack)
    assert plan.can_apply and plan.catalog_identity_binding
    result = native.import_knowledge_pack(root, pack, plan=plan)
    receipt = Path(result['backup']['path']) / 'recovery.json'
    assert not (root / 'synthetic-pack-undo-incarnation.json').exists()
    return root, receipt, plan.catalog_identity_binding


def snapshot(root):
    # Exact owned fixture authority, excluding canonical transient exclusion files.
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and (p.suffix == '.json' or 'knowledge-pack-import' in p.parts)}


def review(data):
    root, receipt, binding = data
    return native.review_completed_knowledge_pack_import(root, receipt, catalog_identity_binding=binding)


def test_complete_review_is_immutable_pinned_and_read_only(completed):
    root, receipt, binding = completed
    before = snapshot(root)
    result = review(completed)
    assert result.catalog_identity_binding == binding
    assert result.receipt_sha256 == sha256(receipt.read_bytes()).hexdigest()
    assert result.receipt_relative == receipt.relative_to(root).as_posix()
    assert result.receipt_epoch and result.default_authority_fingerprints and result.participants
    assert not hasattr(result, 'can_apply') and not hasattr(result, 'undo')
    with pytest.raises(FrozenInstanceError):
        result.receipt_sha256 = 'changed'
    assert review(completed) == result and snapshot(root) == before


@pytest.mark.parametrize('case', ['legacy', 'malformed_header', 'wrong_binding', 'incarnation', 'extensions',
    'schema', 'kind', 'pending', 'restored', 'foundation', 'other_pending',
    'default_changed', 'default_missing', 'backup_changed', 'backup_missing',
    'participant_changed', 'participant_missing', 'already_before', 'outside', 'traversal',
    'receipt_identity', 'default_membership', 'invalid_receipt_json'])
def test_review_refusals_preserve_all_fixture_authority(completed, case):
    root, receipt, binding = completed
    value = json.loads(receipt.read_text(encoding='utf-8'))
    header = root / 'data/user/catalog_identity.json'
    selected = receipt
    if case == 'legacy': header.unlink()
    elif case == 'malformed_header': header.write_bytes(b'{}')
    elif case == 'wrong_binding': binding = tuple((k, 'changed' if k == 'metadata_sha256' else v) for k,v in binding)
    elif case in {'incarnation', 'extensions'}:
        data = json.loads(header.read_text())
        data['incarnation_id' if case == 'incarnation' else 'future'] = (
            '00000000-0000-0000-0000-000000000003' if case == 'incarnation' else 'retained')
        header.write_text(json.dumps(data), encoding='utf-8')
    elif case in {'schema', 'kind', 'pending', 'restored', 'foundation'}:
        if case == 'schema': value['schema_version'] = 2
        elif case == 'kind': value['identity_bound_operation'] = 'pack_undo'
        elif case in {'pending', 'restored'}: value['status'] = case
        else: value['foundation_transition'] = {}
        receipt.write_text(json.dumps(value), encoding='utf-8')
    elif case == 'other_pending':
        other = receipt.parent.parent / 'another-import' / 'recovery.json'
        other.parent.mkdir(); pending = dict(value, status='pending')
        other.write_text(json.dumps(pending), encoding='utf-8')
    elif case == 'receipt_identity':
        value['catalog_identity_binding']['incarnation_id'] = '00000000-0000-0000-0000-000000000003'
        receipt.write_text(json.dumps(value), encoding='utf-8')
    elif case == 'default_membership':
        (root / 'data/defaults/extra_tags.json').write_text('{}', encoding='utf-8')
    elif case == 'invalid_receipt_json': receipt.write_bytes(b'{')
    elif case in {'default_changed', 'default_missing'}:
        name = next(name for name,digest in value['default_authority_fingerprints'] if digest != 'missing')
        target = root / name
        if case == 'default_missing': target.unlink()
        else: target.write_bytes(target.read_bytes() + b' ')
    elif case in {'backup_changed', 'backup_missing'}:
        row = next(row for row in value['files'] if row['existed'])
        target = receipt.parent / row['backup']
        if case == 'backup_missing': target.unlink()
        else: target.write_bytes(b'changed backup')
    elif case in {'participant_changed', 'participant_missing', 'already_before'}:
        row = next(row for row in value['files'] if row['existed'] and row['expected_before'] != row['expected_after'])
        target = Path(row['target'])
        if case == 'participant_missing': target.unlink()
        elif case == 'already_before': target.write_bytes((receipt.parent / row['backup']).read_bytes())
        else: target.write_bytes(target.read_bytes() + b' ')
    elif case == 'outside': selected = root / 'recovery.json'
    elif case == 'traversal': selected = receipt.parent / '..' / receipt.parent.name / 'recovery.json'
    before = snapshot(root)
    with pytest.raises((RuntimeError, ValueError, TypeError, OSError)):
        native.review_completed_knowledge_pack_import(root, selected, catalog_identity_binding=binding)
    assert snapshot(root) == before


def test_receipt_reparse_flag_uses_canonical_refusal(completed, monkeypatch):
    from types import SimpleNamespace
    root, receipt, _binding = completed
    before = snapshot(root)
    original = Path.stat
    def flagged(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self == receipt.parent:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    monkeypatch.setattr(Path, 'stat', flagged)
    with pytest.raises(RuntimeError, match='redirected'):
        review(completed)
    assert snapshot(root) == before


@pytest.mark.parametrize('change', ['receipt', 'history', 'header', 'participant', 'defaults'])
def test_mid_review_drift_is_held_without_inverse(completed, monkeypatch, change):
    root, receipt, binding = completed
    original = native._prepare_pack_restore
    injected = []
    after_drift = []
    def prepare(*args, **kwargs):
        rows = original(*args, **kwargs)
        if not injected:
            injected.append(True)
            if change == 'receipt': receipt.write_bytes(receipt.read_bytes() + b' ')
            elif change == 'history':
                other = receipt.parent.parent / 'new-completed' / 'recovery.json'
                other.parent.mkdir(); other.write_bytes(receipt.read_bytes())
            elif change == 'header':
                header = root / 'data/user/catalog_identity.json'; header.write_bytes(header.read_bytes() + b' ')
            elif change == 'participant': Path(rows[0][0]).write_bytes(Path(rows[0][0]).read_bytes() + b' ')
            else:
                value = json.loads(receipt.read_text()); name = next(n for n,h in value['default_authority_fingerprints'] if h != 'missing')
                target = root / name; target.write_bytes(target.read_bytes() + b' ')
            after_drift.append(snapshot(root))
        return rows
    monkeypatch.setattr(native, '_prepare_pack_restore', prepare)
    with pytest.raises((RuntimeError, ValueError, OSError)):
        review(completed)
    assert injected and snapshot(root) == after_drift[0]
