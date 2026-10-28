"""Formal reference preparation only writes a new, private read-only index."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from core.sales_history_matching import search_candidates
from core.sales_history_reference import SalesHistoryReference

spec = importlib.util.spec_from_file_location('prepare_sales_skill_reference',
    Path(__file__).resolve().parents[1] / 'tools/prepare_sales_skill_reference.py')
tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)


def row(identity='1', name='客户甲', code='OLD1', product='LS1'):
    values = {'order_row_id': identity, 'customer_name': name, 'customer_code': code,
        'erp_code': product, 'order_date': '2026-07-01', 'source_file': 'sales.xls', 'source_excel_row': identity,
        'customer_order_no': 'PO-1', 'project_no': '项目', 'order_line': '1', 'customer_part_no': 'PART-1',
        'customer_product_name': '六角头螺栓', 'customer_standard_spec': 'GB/T5783 M6*30',
        'customer_grade': '8.8', 'customer_surface': '发黑', 'customer_quantity_text': '10',
        'customer_quantity_num': '10', 'is_usable_for_history': '是', 'remark_system_code_matches_erp': '是'}
    values['raw_remark'] = '#'.join(values[key] for key in ('customer_order_no', 'project_no', 'order_line',
        'customer_part_no', 'customer_product_name', 'customer_standard_spec', 'customer_grade',
        'customer_surface', 'customer_quantity_text', 'erp_code'))
    return values


def frozen_source(tmp_path, rows):
    path = (tmp_path / 'source.sqlite').resolve()
    columns = list(rows[0])
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE historical_orders(' + ','.join('"' + key + '" TEXT' for key in columns) + ')')
        db.executemany('INSERT INTO historical_orders VALUES (' + ','.join('?' for _ in columns) + ')',
                       [[value[key] for key in columns] for value in rows])
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def current_database(tmp_path, names=('客户甲', '客户乙')):
    path = (tmp_path / 'current.sqlite').resolve()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('CREATE TABLE records(kind TEXT,id TEXT,payload TEXT,sku TEXT)')
        policy = {'profile_id': 'PROD', 'account_id': 'ACCOUNT', 'customer_bindings': {
            name: {'customer_name': name, 'erp_customer_id': 'ERP' + str(index), 'profile_id': 'PROD', 'account_id': 'ACCOUNT'}
            for index, name in enumerate(names)}}
        db.execute('INSERT INTO settings VALUES (?,?)', ('sales_web_import_policy', json.dumps(policy)))
        product = {'profile_id': 'PROD', 'warehouse_id': 'W', 'warehouse_name': '江苏优螺',
            'product_id': 'product-LS1', 'sku_id': 'sku-LS1', 'base_unit': '1', 'unit_name': '支',
            'product_name': '六角头螺栓', 'spec': 'M6*30', 'standard': 'GB/T5783', 'grade': '8.8', 'surface': '发黑'}
        db.execute('INSERT INTO records VALUES (?,?,?,?)', ('inventory', 'R1', json.dumps(product), 'LS1'))
    return path


def prepare(tmp_path, rows=None):
    source, digest = frozen_source(tmp_path, rows or [row()])
    current = current_database(tmp_path)
    output = (tmp_path / 'new-reference/sales-matching.sqlite3').resolve()
    return source, digest, current, output


def test_formal_projection_uses_verified_profile_without_modifying_source_or_business_db(tmp_path):
    source, digest, current, output = prepare(tmp_path)
    current_bytes = current.read_bytes()
    result = tool.prepare_reference(source, digest, current, output)
    assert result['imported_rows'] == result['eligible_rows'] == 1
    assert result['bound_customer_count'] == 1 and result['verified_current_customer_count'] == 2
    assert result['business_db_written'] is False and result['erp_written'] is False
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest and current.read_bytes() == current_bytes
    assert output.stat().st_mode & 0o777 == 0o400 and output.parent.stat().st_mode & 0o777 == 0o700
    reference = SalesHistoryReference(output, result['reference_sha256'])
    db = sqlite3.connect(current)
    with reference.connect() as history:
        matched = search_candidates(db, 'PROD', '客户甲', {'customer_part_no': 'PART-1'}, history_db=history)
        assert matched['candidates'][0]['available']
        assert matched['coverage']['customer_identity_verified'] is True
        assert 'customer_binding_proofs' not in matched['coverage']
        assert history.execute('SELECT DISTINCT profile_id FROM sales_matching_sources').fetchall()[0][0] == 'PROD'


@pytest.mark.parametrize('rows', [
    [row(), row('2', code='OLD2'), row('3', name='客户乙', code='OLD3')],
    [row(), row('2', name='旧名称', code='OLD1'), row('3', name='客户乙', code='OLD3')],
])
def test_historical_name_or_code_conflicts_are_kept_for_review_not_merged(tmp_path, rows):
    source, digest, current, output = prepare(tmp_path, rows)
    result = tool.prepare_reference(source, digest, current, output)
    assert result['bound_customer_count'] == 1 and result['unbound_history_rows'] == 2
    with sqlite3.connect(output) as db:
        preserved = db.execute('SELECT count(*) FROM sales_matching_history').fetchone()[0]
        assert preserved == 3
        customer_a = db.execute("SELECT eligible,review_reasons FROM sales_matching_history WHERE customer_name='客户甲'").fetchall()
        assert all(not eligible and 'historical_customer_identity_unbound' in reasons for eligible, reasons in customer_a)


def test_unknown_current_customer_is_not_linked_by_name_contains(tmp_path):
    source, digest, current, output = prepare(tmp_path, [row(), row('2', name='客户甲分公司', code='OTHER')])
    result = tool.prepare_reference(source, digest, current, output)
    assert result['eligible_rows'] == 1 and result['unbound_history_rows'] == 1


def test_customer_id_change_blocks_historical_candidates_and_preserves_reference(tmp_path):
    source, digest, current, output = prepare(tmp_path)
    result = tool.prepare_reference(source, digest, current, output)
    before = output.read_bytes()
    db = sqlite3.connect(current)
    policy = json.loads(db.execute("SELECT value FROM settings WHERE key='sales_web_import_policy'").fetchone()[0])
    policy['customer_bindings']['客户甲']['erp_customer_id'] = 'DIFFERENT'
    db.execute("UPDATE settings SET value=? WHERE key='sales_web_import_policy'", (json.dumps(policy),)); db.commit()
    reference = SalesHistoryReference(output, result['reference_sha256'])
    with reference.connect() as history:
        matched = search_candidates(db, 'PROD', '客户甲', {'customer_part_no': 'PART-1'}, history_db=history)
        assert matched['candidates'] == []
        assert matched['review_reasons'] == ['history_current_customer_identity_changed']
    assert output.read_bytes() == before


def test_reference_never_overwrites_an_existing_output(tmp_path):
    source, digest, current, output = prepare(tmp_path)
    tool.prepare_reference(source, digest, current, output)
    before = output.read_bytes()
    with pytest.raises(ValueError, match='new_independent_output_required'):
        tool.prepare_reference(source, digest, current, output)
    assert output.read_bytes() == before


def test_hash_mismatch_and_profile_mismatch_fail_before_creating_output(tmp_path):
    source, digest, current, output = prepare(tmp_path)
    with pytest.raises(ValueError, match='source_hash_mismatch'):
        tool.prepare_reference(source, '0' * 64, current, output)
    with sqlite3.connect(current) as db:
        payload = json.loads(db.execute('SELECT payload FROM records').fetchone()[0]); payload['profile_id'] = 'OTHER'
        db.execute('UPDATE records SET payload=?', (json.dumps(payload),))
    with pytest.raises(ValueError, match='current_profile_mismatch'):
        tool.prepare_reference(source, digest, current, output)
    assert not output.exists() and not output.parent.exists()


def test_unverified_customer_policy_is_not_reconstructed_from_old_followup(tmp_path):
    source, digest, current, output = prepare(tmp_path)
    with sqlite3.connect(current) as db:
        db.execute("DELETE FROM settings WHERE key='sales_web_import_policy'")
    with pytest.raises(ValueError, match='customer_policy_missing'):
        tool.prepare_reference(source, digest, current, output)
    assert not output.exists()
