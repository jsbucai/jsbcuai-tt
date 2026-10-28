"""Sales worker candidates remain bounded, provenance-bound and read-only."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from core.sales_history_matching import import_history_snapshot
from core.sales_skill_matching import (build_matching_context, catalog_attributes,
                                       load_skill_rules)


RULES_DIR = Path(__file__).resolve().parents[2] / 'deploy/skills/ul-sales-order/references/rules'


@pytest.fixture
def rules():
    return load_skill_rules(RULES_DIR)


def database():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE records(id TEXT,sku TEXT,payload TEXT,kind TEXT)')
    return db


def product(db, code='LS1', record='R1', **changes):
    value = {'profile_id': 'P', 'warehouse_id': 'W', 'warehouse': '江苏优螺',
        'product_id': 'product-' + code, 'sku_id': 'sku-' + code, 'base_unit': '1',
        'product_name': '六角头螺栓', 'spec': 'M6*30', 'model': '8.8级 发黑',
        'standard_no': 'GB/T5783', 'unit_name': '支', 'material': '碳钢'}
    value.update(changes)
    db.execute('INSERT INTO records VALUES (?,?,?,?)', (record, code, json.dumps(value), 'inventory'))
    return value


def source(identity='S1', **changes):
    ref = {'id': identity, 'original_name': '六角头螺栓', 'original_spec': 'M6×30',
           'original_grade': '8.8', 'original_surface': '发黑', 'original_standard': 'GB/T5783'}
    ref.update(changes)
    return {'source_ref': ref}


def historical(identity='1', customer='客户甲', code='LS1', part='PART-1', **changes):
    row = {'order_row_id': identity, 'customer_name': customer, 'customer_code': 'customer-code',
        'erp_code': code, 'order_date': '2026-07-01', 'source_file': 'frozen-sales.xls', 'source_excel_row': identity,
        'customer_order_no': 'PO-1', 'project_no': '项目甲', 'order_line': '1', 'customer_part_no': part,
        'customer_product_name': '六角头螺栓', 'customer_standard_spec': 'GB/T5783 M6*30',
        'customer_grade': '8.8', 'customer_surface': '发黑', 'customer_quantity_text': '10',
        'customer_quantity_num': '10', 'is_usable_for_history': '是', 'remark_system_code_matches_erp': '是'}
    row.update(changes)
    row['raw_remark'] = '#'.join([row[key] for key in ('customer_order_no', 'project_no', 'order_line',
        'customer_part_no', 'customer_product_name', 'customer_standard_spec', 'customer_grade',
        'customer_surface', 'customer_quantity_text', 'erp_code')])
    return row


def history_reference(tmp_path, rows):
    path = tmp_path / 'raw-history.sqlite'
    columns = list(rows[0])
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE historical_orders(' + ','.join('"' + key + '" TEXT' for key in columns) + ')')
        db.executemany('INSERT INTO historical_orders VALUES (' + ','.join('?' for _ in columns) + ')',
                       [[row.get(key) for key in columns] for row in rows])
    reference = sqlite3.connect(':memory:')
    import_history_snapshot(reference, path, 'P', hashlib.sha256(path.read_bytes()).hexdigest())
    return reference


def context(db, lines=None, **kwargs):
    return build_matching_context(db, 'P', '客户甲', lines or [source()], **kwargs)


def test_original_rules_are_pinned_and_modified_file_rejected(tmp_path, rules):
    assert len(rules.hashes) == 6 and rules.documents['客户料号规则.json']['default_policy']['allow_global_part_match'] is False
    for file in RULES_DIR.glob('*.json'):
        (tmp_path / file.name).write_bytes(file.read_bytes())
    file = tmp_path / '商品名称同义词.json'
    file.write_text(file.read_text() + ' ')
    with pytest.raises(ValueError, match='hash_mismatch'):
        load_skill_rules(tmp_path)


def test_catalog_unique_explicit_model_tokens_and_standard_no_preserve_input():
    value = {'product_name': '螺栓', 'spec': 'M6*30', 'model': '8.8级 发黑', 'standard_no': 'GB/T5783'}
    before = copy.deepcopy(value)
    attrs = catalog_attributes(value)
    assert value == before
    assert attrs['grade'] == '8.8' and attrs['surface'] == '发黑' and attrs['standard'] == 'GB/T5783'
    assert attrs['attribute_provenance']['grade']['field'] == 'model'
    assert attrs['attribute_provenance']['standard']['field'] == 'standard_no'


@pytest.mark.parametrize('model,field,reason', [
    ('8.8/10.9 发黑', 'grade', 'multiple_grade_tokens'),
    ('8.8 发黑/镀锌', 'surface', 'multiple_surface_tokens'),
])
def test_conflicting_model_tokens_are_not_guessed(model, field, reason):
    attrs = catalog_attributes({'model': model})
    assert attrs[field] == '' and reason in attrs['attribute_review_reasons']


def test_missing_grade_finish_does_not_receive_a_default():
    attrs = catalog_attributes({'model': '客户专用'})
    assert attrs['grade'] == '' and attrs['surface'] == ''


def test_explicit_fields_are_not_overwritten_from_model():
    attrs = catalog_attributes({'grade': '12.9', 'surface': '达克罗', 'standard': 'DIN933', 'model': '8.8 发黑'})
    assert (attrs['grade'], attrs['surface'], attrs['standard']) == ('12.9', '达克罗', 'DIN933')


def test_current_catalog_without_history_generates_a_draft_candidate_readonly(rules):
    db = database(); product(db)
    before = db.total_changes
    lines = [source()]; original = copy.deepcopy(lines)
    result = context(db, lines, rules=rules)
    candidate = result['lines'][0]['candidates'][0]
    assert candidate['available'] and candidate['product_code'] == 'LS1'
    assert candidate['method'] == 'current_catalog_attributes'
    assert candidate['current_identity']['base_unit'] == '1'
    assert result['lines'][0]['suggested_product_code'] == 'LS1'
    assert result['automatic_submission'] is False and result['history_prices_used'] is False
    assert result['history_coverage']['available'] is False
    assert lines == original and db.total_changes == before
    assert not any(field in json.dumps(result).lower() for field in ('unit_price', 'taxed_price'))


def test_catalog_is_scanned_once_for_multi_line_batch(rules):
    db = database(); product(db)
    queries = []
    db.set_trace_callback(queries.append)
    result = context(db, [source('S1'), source('S2'), source('S3')], rules=rules)
    assert len(result['lines']) == 3
    assert sum("SELECT id,sku,payload FROM records WHERE kind='inventory' ORDER BY" in query for query in queries) == 1


@pytest.mark.parametrize('changes', [{'status': '停用'}, {'status': '已删除'},
    {'isstop': True}, {'deleted': True}, {'is_stopped': True}, {'is_deleted': True}])
def test_current_chinese_and_raw_api_inactive_facts_block_candidates(rules, changes):
    db = database(); product(db, **changes)
    candidate = context(db, rules=rules)['lines'][0]['candidates'][0]
    assert not candidate['available'] and 'current_product_inactive' in candidate['review_reasons']


def cache_inactive(db, *, hash_valid=True, profile='P', duplicate=False):
    db.execute('CREATE TABLE erp_reference_cache(kind TEXT,profile TEXT,payload TEXT,fetched_at TEXT,source_hash TEXT)')
    products = [{'ptypeid': 'product-LS1', 'deleted': True}]
    if duplicate:
        products.append({'ptypeid': 'product-LS1', 'deleted': False})
    payload = json.dumps({'products': products}, ensure_ascii=False, separators=(',', ':'))
    db.execute('INSERT INTO erp_reference_cache VALUES (?,?,?,?,?)', ('catalog-v1', profile, payload,
        datetime.now(timezone.utc).isoformat(), hashlib.sha256(payload.encode()).hexdigest() if hash_valid else 'x' * 64))


def test_integrity_checked_current_api_cache_blocks_omitted_inventory_deleted_flag(rules):
    db = database(); product(db); cache_inactive(db)
    before = db.total_changes
    candidate = context(db, rules=rules)['lines'][0]['candidates'][0]
    assert not candidate['available'] and 'current_product_inactive' in candidate['review_reasons']
    assert db.total_changes == before


@pytest.mark.parametrize('options', [{'hash_valid': False}, {'profile': 'OTHER'}, {'duplicate': True}])
def test_other_profile_damaged_or_partial_cache_is_not_positive_inactive_evidence(rules, options):
    db = database(); product(db); cache_inactive(db, **options)
    assert context(db, rules=rules)['lines'][0]['candidates'][0]['available']


def test_exact_customer_history_uses_derived_api_model_without_history_prices(tmp_path, rules):
    db = database(); product(db)
    reference = history_reference(tmp_path, [historical()])
    before = (db.total_changes, reference.total_changes)
    result = context(db, [source(customer_part_no='PART-1')], rules=rules, history_db=reference)
    candidate = result['lines'][0]['candidates'][0]
    assert candidate['method'] == 'customer_part_exact' and candidate['available']
    assert candidate['evidence_count'] == 1 and candidate['source_evidence'][0]['source_excel_row'] == '1'
    assert (db.total_changes, reference.total_changes) == before


def test_ordinary_customer_part_never_falls_back_across_customer(tmp_path, rules):
    db = database(); product(db)
    reference = history_reference(tmp_path, [historical(customer='客户乙')])
    result = context(db, [{'source_line_id': 'S1', 'customer_part_no': 'PART-1'}], rules=rules, history_db=reference)
    assert result['lines'][0]['candidates'] == []


def test_nas_explicit_danieli_part_pattern_can_share_qualified_group_evidence(tmp_path, rules):
    db = database(); product(db)
    reference = history_reference(tmp_path, [historical(customer='达涅利配件客户', part='0.000452.M')])
    result = context(db, [source(customer_part_no='0.000452.M')], rules=rules, history_db=reference)
    item = result['lines'][0]['candidates'][0]
    assert item['method'] == 'nas_declared_group_part' and item['available']


def test_nas_group_part_multiple_codes_stays_ambiguous(tmp_path, rules):
    db = database(); product(db); product(db, 'LS2', 'R2')
    reference = history_reference(tmp_path, [historical(customer='达涅利客户', part='0.000452.M'),
        historical('2', customer='另一个达涅利客户', code='LS2', part='0.000452.M')])
    result = context(db, [source(customer_part_no='0.000452.M')], rules=rules, history_db=reference)
    assert result['lines'][0]['suggested_product_code'] == ''
    assert all(not item['available'] for item in result['lines'][0]['candidates'])
    assert result['lines'][0]['history_review_reasons'] == ['nas_group_part_multiple_erp_codes']


def test_declared_gb5781_relation_is_scoped_and_preserves_original(rules):
    db = database(); product(db)
    result = context(db, [source(original_standard='GB/T5781')], rules=rules)
    item = result['lines'][0]['candidates'][0]
    assert item['available'] and item['applied_rules']
    assert result['lines'][0]['source_attributes']['standard'] == 'GB/T5781'
    assert item['attribute_sources']['standard'] == 'GB/T5783'


def test_distinct_din_gb_standards_are_not_merged(rules):
    db = database(); product(db)
    result = context(db, [source(original_standard='DIN933')], rules=rules)
    assert result['lines'][0]['candidates'] == []


def test_finish_synonym_recalls_and_preserves_original_finish(rules):
    db = database(); product(db, model='8.8级 达克罗')
    result = context(db, [source(original_surface='复合涂层')], rules=rules)
    item = result['lines'][0]['candidates'][0]
    assert item['available']
    assert result['lines'][0]['source_attributes']['surface'] == '复合涂层'
    assert item['attribute_sources']['surface'] == '达克罗'


def test_missing_material_does_not_apply_carbon_steel_200hv_rule(rules):
    db = database()
    product(db, product_name='平垫圈', spec='M6', model='200HV 发黑', standard_no='GB/T97.1')
    line = source(original_name='平垫圈', original_spec='M6', original_grade='140HV', original_standard='GB/T97.1')
    assert context(db, [line], rules=rules)['lines'][0]['candidates'] == []


def test_200hv_rule_only_for_explicit_carbon_standard_plain_washer(rules):
    db = database()
    product(db, product_name='平垫圈', spec='M6', model='200HV 发黑', standard_no='GB/T97.1')
    line = source(original_name='平垫圈', original_spec='M6', original_grade='140HV',
                  original_standard='GB/T97.1', original_material='碳钢')
    item = context(db, [line], rules=rules)['lines'][0]['candidates'][0]
    assert item['available'] and item['applied_rules'] == ['nas_allowed_grade:FLAT_WASHER_140HV_TO_200HV']
    product(db, 'LS2', 'R2', product_name='平垫圈', spec='M6', model='200HV 发黑',
            standard_no='GB/T97.1', material='304')
    assert {item['product_code'] for item in context(db, [line], rules=rules)['lines'][0]['candidates']} == {'LS1'}


@pytest.mark.parametrize('changes', [{'is_stopped': True}, {'base_unit': '2'}, {'sku_id': ''},
                                    {'warehouse': '其他仓库'}, {'profile_id': 'OTHER'}])
def test_inactive_wrong_warehouse_profile_unit_or_incomplete_identity_never_usable(changes, rules):
    db = database(); product(db, **changes)
    result = context(db, rules=rules)
    assert not any(item['available'] for item in result['lines'][0]['candidates'])


def test_same_code_duplicate_current_identities_are_not_chosen(rules):
    db = database(); product(db); product(db, record='R2')
    assert context(db, rules=rules)['lines'][0]['candidates'] == []


def test_same_item_separate_source_rows_do_not_merge_and_candidates_bound_to_five(rules):
    db = database()
    for index in range(8):
        product(db, f'LS{index}', f'R{index}')
    result = context(db, [source('S1'), source('S2')], rules=rules)
    assert [line['source_line_id'] for line in result['lines']] == ['S1', 'S2']
    assert all(len(line['candidates']) == 5 and line['candidate_count'] == 8 for line in result['lines'])
    assert all(line['suggested_product_code'] == '' for line in result['lines'])


@pytest.mark.parametrize('lines', [[source('S1'), source('S1')], [{'original_spec': 'M6'}]])
def test_missing_or_duplicate_source_ids_rejected(lines, rules):
    db = database(); product(db)
    with pytest.raises(ValueError, match='source_line_identity_invalid'):
        context(db, lines, rules=rules)


def test_both_flat_and_ref_source_shapes_supported(rules):
    db = database(); product(db)
    flat = {'source_line_id': 'S1', **{key: value for key, value in source()['source_ref'].items() if key != 'id'}}
    ref = {'_ref': {**source('S2')['source_ref']}}
    result = context(db, [flat, ref], rules=rules)
    assert all(line['suggested_product_code'] == 'LS1' for line in result['lines'])
