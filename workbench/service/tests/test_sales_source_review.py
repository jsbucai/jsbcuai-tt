"""Pure source review tests; no server, ERP writes or browser access."""
from io import BytesIO

from openpyxl import Workbook
import pytest

from core.sales_source_parser import parse_excel
from core.sales_source_review import (inspect_source_match, make_source_confirmation,
                                      source_confirmation_valid)


IDENTITY = {'profile_id': 'P', 'warehouse_id': 'W', 'product_id': 'product-1',
            'sku_id': 'sku-1', 'base_unit': '1'}
CATALOG = {'product_name': '内六角圆柱头螺钉', 'spec': 'M6*30', 'model': '原始型号',
           'grade': '8.8级', 'surface': '发黑'}
SOURCE = {'id': 'spreadsheet:file:0:2', 'kind': 'spreadsheet', 'source_id': 'file',
          'sha256': 'a' * 64, 'sheet': 'Sheet1', 'row': 2,
          'preview': '原文', 'original_name': CATALOG['product_name'],
          'original_spec': 'M6×30', 'original_grade': '8.8', 'original_surface': '发黑'}
NOW = '2026-10-07T17:30:00+08:00'


def inspect(source=None, catalog=None, identity=None):
    return inspect_source_match(SOURCE if source is None else source,
                                CATALOG if catalog is None else catalog,
                                IDENTITY if identity is None else identity)


def test_proven_size_symbols_and_grade_suffix_keep_raw_facts():
    result = inspect()
    assert result['requires_confirmation'] is False
    assert result['source']['original_spec'] == 'M6×30'
    assert result['catalog']['spec'] == 'M6*30'
    assert {check['field']: check['status'] for check in result['checks']} == {
        'name': 'matched', 'spec': 'matched_size_symbols',
        'grade': 'matched_grade_suffix', 'surface': 'matched'}


def test_explicit_material_is_bound_but_empty_material_preserves_old_review():
    old=inspect()
    assert inspect({**SOURCE,'original_material':''}, {**CATALOG,'material':'碳钢'})['revision']==old['revision']
    matched=inspect({**SOURCE,'original_material':'碳钢'}, {**CATALOG,'material':'碳钢'})
    assert not matched['requires_confirmation'] and matched['revision']!=old['revision']
    changed=inspect({**SOURCE,'original_material':'碳钢'}, {**CATALOG,'material':'中国'})
    assert changed['requires_confirmation'] and changed['revision']!=matched['revision']


@pytest.mark.parametrize('source_spec,catalog_spec', [
    ('M6 X 30', 'm6*30'), ('M6.0×30.00', 'M6X30'),
])
def test_only_complete_metric_dimensions_have_equivalence(source_spec, catalog_spec):
    assert not inspect({**SOURCE, 'original_spec': source_spec},
                       {**CATALOG, 'spec': catalog_spec})['requires_confirmation']


@pytest.mark.parametrize('source_spec,catalog_spec', [
    ('DIN 933 M6*30', 'GB/T5783 M6*30'),
    ('M6*30-8.8', 'M6*30'), ('M6*30', 'M6*35'),
    ('1/4X30', 'M6*30'), ('M6', 'M6*30'),
])
def test_standards_partial_and_different_sizes_require_review(source_spec, catalog_spec):
    result = inspect({**SOURCE, 'original_spec': source_spec}, {**CATALOG, 'spec': catalog_spec})
    assert result['requires_confirmation']
    assert next(row for row in result['checks'] if row['field'] == 'spec')['status'] == 'differs'


def test_name_abbreviation_is_a_human_review_not_an_automatic_identity_merge():
    result = inspect({**SOURCE, 'original_name': '内六角圆柱头'})
    assert result['requires_confirmation']
    assert result['checks'][0]['status'] == 'possible_abbreviation'
    contradictory = inspect({**SOURCE, 'original_name': '垫圈'})
    assert contradictory['requires_confirmation']
    assert contradictory['checks'][0]['status'] == 'differs'


def test_missing_grade_and_finish_are_not_guessed_from_mixed_model():
    result = inspect(catalog={**CATALOG, 'grade': '', 'surface': '', 'model': '8.8级发黑'})
    assert result['requires_confirmation']
    assert [check['status'] for check in result['checks'][2:]] == ['catalog_missing', 'catalog_missing']
    assert result['catalog']['model'] == '8.8级发黑'


def test_missing_source_descriptions_do_not_claim_a_conflict():
    result = inspect({key: value for key, value in SOURCE.items() if not key.startswith('original_')})
    assert not result['requires_confirmation']
    assert all(check['status'] == 'not_provided' for check in result['checks'])


@pytest.mark.parametrize('given,model,requires', [
    ('DIN 933','DIN 933',False),
    ('DIN 933','GB/T 5783',True),
    ('GB/T 5783','GB 5783',True),
    ('DIN 933','8.8级发黑',True),
])
def test_separate_source_standard_requires_exact_catalog_evidence(given,model,requires):
    review=inspect({**SOURCE,'original_standard':given},{**CATALOG,'model':model})
    assert review['requires_confirmation'] is requires
    check=review['checks'][-1]
    assert check['field']=='standard' and check['source']==given
    record=make_source_confirmation(review,'sales-1',NOW)
    changed=inspect({**SOURCE,'original_standard':given},{**CATALOG,'model':model,'standard':'ISO 4017'})
    assert not source_confirmation_valid(record,changed)


@pytest.mark.parametrize('kind,key,value', [
    ('source', 'sha256', 'b' * 64), ('source', 'original_spec', 'M8*30'),
    ('source', 'original_grade', '4.8'), ('source', 'original_surface', '镀锌'),
    ('source', 'original_name', '新名称'), ('source', 'row', 3),
    ('catalog', 'product_name', '新名称'), ('catalog', 'spec', 'M8*30'),
    ('catalog', 'model', '新的原始型号'), ('catalog', 'grade', '10.9'),
    ('catalog', 'surface', '镀锌'), ('identity', 'sku_id', 'sku-2'),
    ('identity', 'warehouse_id', 'warehouse-2'), ('identity', 'base_unit', '2'),
])
def test_source_or_catalog_change_invalidates_server_confirmation(kind, key, value):
    review = inspect()
    record = make_source_confirmation(review, 'sales-1', NOW)
    assert source_confirmation_valid(record, review)
    source, catalog, identity = dict(SOURCE), dict(CATALOG), dict(IDENTITY)
    {'source': source, 'catalog': catalog, 'identity': identity}[kind][key] = value
    new_review = inspect(source, catalog, identity)
    assert new_review['revision'] != review['revision']
    assert not source_confirmation_valid(record, new_review)


def test_confirmation_keeps_server_actor_and_requires_valid_clock():
    review = inspect({**SOURCE, 'original_spec': 'M8*30'})
    record = make_source_confirmation(review, 'sales-1', NOW)
    assert record == {'schema': 'sales_source_match_v1', 'revision': review['revision'],
                      'confirmed_by': 'sales-1', 'confirmed_at': NOW}
    assert source_confirmation_valid(record, review)
    assert not source_confirmation_valid(True, review)
    assert not source_confirmation_valid({**record, 'confirmed_at': '2026-10-07'}, review)
    with pytest.raises(ValueError):
        make_source_confirmation(review, '', NOW)
    with pytest.raises(ValueError):
        make_source_confirmation(review, 'sales-1', '2026-10-07T17:30:00')


def test_excel_grade_and_surface_are_preserved_in_source_not_silently_applied():
    book = Workbook(); sheet = book.active
    sheet.append(['商品编码', '商品名称', '规格', '等级', '表面处理', '数量', '单价'])
    sheet.append(['LS1000', '垫圈', 'M8*30', '4.8', '发黑', 1, 1.13])
    raw = BytesIO(); book.save(raw)
    parsed, reports = parse_excel(raw.getvalue(),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'file', 'a' * 64, '客户甲')
    assert len(parsed) == 1 and len(reports) == 2
    source = parsed[0]['source_ref']
    assert (source['original_name'], source['original_spec'], source['original_grade'],
            source['original_surface']) == ('垫圈', 'M8*30', '4.8', '发黑')
    assert 'grade' not in parsed[0] and 'surface' not in parsed[0]
    assert inspect(source)['requires_confirmation']
