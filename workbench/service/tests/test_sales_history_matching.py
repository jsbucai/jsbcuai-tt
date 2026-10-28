"""Historical candidates use isolated synthetic references, never ERP writes."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from core.sales_history_matching import (file_sha256, import_history_snapshot,
                                         search_candidates)


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sales_history_index_import', ROOT/'tools'/'sales_history_index_import.py')
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)


def historical(identity='1',customer='客户甲',code='LS1',part='PART-1',project='项目甲',**changes):
    row={'order_row_id':identity,'customer_name':customer,'customer_code':'customer-code',
        'erp_code':code,'order_date':'2026-07-01','source_file':'frozen-sales.xls','source_excel_row':identity,
        'customer_order_no':'PO-1','project_no':project,'order_line':'1','customer_part_no':part,
        'customer_product_name':'六角头螺栓','customer_standard_spec':'DIN 933 M6*30',
        'customer_grade':'8.8','customer_surface':'发黑','customer_quantity_text':'10',
        'customer_quantity_num':'10','is_usable_for_history':'是','remark_system_code_matches_erp':'是'}
    row['raw_remark']='#'.join([row['customer_order_no'],row['project_no'],row['order_line'],part,
        row['customer_product_name'],row['customer_standard_spec'],row['customer_grade'],row['customer_surface'],
        row['customer_quantity_text'],code])
    row.update(changes)
    return row


def snapshot(tmp_path,rows):
    path=tmp_path/'reference.sqlite'
    columns=list(rows[0])
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE historical_orders('+','.join('"'+c+'" TEXT' for c in columns)+')')
        db.executemany('INSERT INTO historical_orders VALUES ('+','.join('?' for _ in columns)+')',
                       [[row.get(c) for c in columns] for row in rows])
    return path,file_sha256(path)


def database():
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE records(kind TEXT,id TEXT,payload TEXT,sku TEXT)')
    return db


def product(db,code='LS1',record='record-1',**changes):
    value={'profile_id':'P','warehouse_id':'W','warehouse_name':'江苏优螺','product_id':'product-'+code,
        'sku_id':'sku-'+code,'base_unit':'1','product_name':'六角头螺栓','spec':'M6*30',
        'model':'DIN 933','grade':'8.8级','surface':'发黑','unit_name':'支'}
    value.update(changes)
    db.execute('INSERT INTO records VALUES (?,?,?,?)',('inventory',record,json.dumps(value,ensure_ascii=False),code))


def prepared(tmp_path,rows=None):
    path,digest=snapshot(tmp_path,rows or [historical()])
    db=database(); product(db)
    result=import_history_snapshot(db,path,'P',digest)
    return db,path,digest,result


def test_search_missing_reference_is_readonly_and_explicit():
    db=database(); before=db.total_changes
    result=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})
    assert result['status']=='not_prepared' and result['coverage']['available'] is False
    assert db.total_changes==before
    assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'sales_matching_%'").fetchall()


def test_exact_part_candidate_has_current_identity_and_source_without_prices(tmp_path):
    db,path,digest,imported=prepared(tmp_path)
    before=db.total_changes
    result=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})
    item=result['candidates'][0]
    assert item['available'] and item['product_code']=='LS1'
    assert item['current_identity']['base_unit']=='1'
    assert item['method']=='customer_part_exact' and item['requires_confirmation']
    assert item['source_evidence'][0]['source_sha256']==digest
    assert item['source_evidence'][0]['source_excel_row']=='1'
    assert item['last_order_date']==result['covered_to']=='2026-07-01'
    assert not any(key in json.dumps(result).lower() for key in ('unit_price','taxed_price','quotation'))
    assert imported['prices_imported'] is False
    assert db.total_changes==before


def test_raw_legacy_summary_preserves_outer_whitespace_and_newlines(tmp_path):
    row=historical();row['raw_remark']=' \n'+row['raw_remark']+'\n '
    db,_,_,_=prepared(tmp_path,[row])
    stored=db.execute('SELECT raw_summary FROM sales_matching_history').fetchone()[0]
    assert stored==row['raw_remark']
    candidate=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})['candidates'][0]
    assert candidate['available'] and candidate['source_evidence'][0]['raw_summary']==row['raw_remark']


def test_multiple_skus_for_same_exact_customer_part_stay_ambiguous(tmp_path):
    rows=[historical(),historical('2',code='LS2')]
    db,_,_,_=prepared(tmp_path,rows);product(db,'LS2','record-2')
    result=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})
    assert {c['product_code'] for c in result['candidates']}=={'LS1','LS2'}
    assert result['review_reasons']==['same_customer_multiple_erp_codes']
    assert result['automatic_selection'] is False
    assert all(c['requires_confirmation'] for c in result['candidates'])


@pytest.mark.parametrize('customer,part', [('客户乙','PART-1'),('客户甲分公司','PART-1'),('客户甲','PART1'),('客户甲','part-1')])
def test_no_cross_customer_contains_or_broad_part_normalization(tmp_path,customer,part):
    db,_,_,_=prepared(tmp_path)
    assert search_candidates(db,'P',customer,{'customer_part_no':part})['candidates']==[]
    assert search_candidates(db,'different-profile','客户甲',{'customer_part_no':'PART-1'})['status']=='not_prepared'


def test_same_customer_attributes_are_strict_not_name_search(tmp_path):
    db,_,_,_=prepared(tmp_path)
    source={'original_name':'六角头螺栓','original_spec':'DIN 933 M6×30',
            'original_grade':'8.8级','original_surface':'发黑'}
    result=search_candidates(db,'P','客户甲',source)
    assert result['candidates'][0]['available']
    assert result['candidates'][0]['method']=='same_customer_history_attributes'
    assert not search_candidates(db,'P','客户甲',{**source,'original_spec':'DIN 933 M6*35'})['candidates']
    assert not search_candidates(db,'P','客户甲',{**source,'original_spec':'GB/T 5783 M6*30'})['candidates']
    assert search_candidates(db,'P','客户甲',{'original_name':'螺栓'})['status']=='needs_review'


def test_missing_part_may_fall_back_only_to_same_customer_exact_attributes(tmp_path):
    db,_,_,_=prepared(tmp_path)
    source={'customer_part_no':'NEW-PART','original_name':'六角头螺栓','original_spec':'DIN 933 M6×30',
            'original_grade':'8.8','original_surface':'发黑'}
    result=search_candidates(db,'P','客户甲',source)
    assert result['candidates'][0]['method']=='same_customer_history_attributes'
    assert result['candidates'][0]['available']
    assert result['review_reasons']==['exact_customer_part_not_found']
    assert search_candidates(db,'P','客户乙',source)['candidates']==[]


@pytest.mark.parametrize('changes,reason', [
    ({'is_stopped':1},'current_product_inactive'),
    ({'is_deleted':'是'},'current_product_inactive'),
    ({'base_unit':'2'},'current_unit_unverified'),
    ({'warehouse_name':'别的仓库'},'current_product_missing'),
    ({'grade':''},'catalog_grade_missing'),
    ({'spec':'M6*35'},'catalog_spec_conflict'),
    ({'surface':'镀锌'},'catalog_surface_conflict'),
    ({'sku_id':''},'current_product_identity_incomplete'),
])
def test_inactive_unknown_or_conflicting_current_catalog_not_available(tmp_path,changes,reason):
    path,digest=snapshot(tmp_path,[historical()]);db=database();product(db,**changes)
    import_history_snapshot(db,path,'P',digest)
    item=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})['candidates'][0]
    assert item['available'] is False and reason in item['review_reasons']


def test_duplicate_current_identity_is_not_arbitrarily_chosen(tmp_path):
    db,_,_,_=prepared(tmp_path);product(db,record='record-duplicate')
    item=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})['candidates'][0]
    assert not item['available'] and 'current_product_identity_ambiguous' in item['review_reasons']


def test_separate_reference_connection_still_uses_current_catalog_and_writes_neither(tmp_path):
    path,digest=snapshot(tmp_path,[historical()])
    reference=sqlite3.connect(':memory:');import_history_snapshot(reference,path,'P',digest)
    current=database();product(current)
    before=(reference.total_changes,current.total_changes)
    item=search_candidates(current,'P','客户甲',{'customer_part_no':'PART-1'},history_db=reference)['candidates'][0]
    assert item['available'] and item['current_identity']['profile_id']=='P'
    assert (reference.total_changes,current.total_changes)==before
    assert search_candidates(current,'P','客户甲',{'customer_part_no':'PART-1'})['status']=='not_prepared'
    current.execute('DELETE FROM records')
    assert not search_candidates(current,'P','客户甲',{'customer_part_no':'PART-1'},history_db=reference)['candidates'][0]['available']


@pytest.mark.parametrize('changes,reason', [
    ({'remark_system_code_matches_erp':'否'},'history_system_code_conflict'),
    ({'raw_remark':historical()['raw_remark'].replace('#10#LS1','#12#LS1')},'history_raw_quantity_conflict'),
    ({'raw_remark':historical()['raw_remark'].replace('#LS1','#LS9')},'history_raw_sku_conflict'),
    ({'customer_grade':'4.8'},'history_parsed_field_conflict:customer_grade'),
    ({'is_usable_for_history':'否'},'history_marked_unusable'),
    ({'summary_review_required':'是'},'history_summary_conflict'),
])
def test_conflicting_remark_never_enters_reliable_candidates(tmp_path,changes,reason):
    db,_,_,result=prepared(tmp_path,[historical(**changes)])
    assert result['eligible_rows']==0
    item=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})['candidates'][0]
    assert not item['available'] and reason in item['review_reasons']


def test_project_with_hash_is_validated_from_right_not_guessed(tmp_path):
    row=historical(project='项目#设备#附件')
    row['raw_remark'] += '#交期=2026-07-15'
    db,_,_,result=prepared(tmp_path,[row])
    assert result['eligible_rows']==1
    assert search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1'})['candidates'][0]['available']


def test_additional_note_never_becomes_history_or_source_description(tmp_path):
    db,_,_,_=prepared(tmp_path)
    result=search_candidates(db,'P','客户甲',{'extra_note':historical()['raw_remark'],'note':historical()['raw_remark']})
    assert result['candidates']==[] and result['status']=='needs_review'
    result=search_candidates(db,'P','客户甲',{'customer_part_no':'PART-1','summary_source':'conflict'})
    assert result['candidates']==[] and result['review_reasons']==['source_summary_conflict']


def test_duplicate_import_is_noop_and_preserves_other_tables(tmp_path):
    db,path,digest,_=prepared(tmp_path)
    before=list(db.execute('SELECT * FROM records'))
    result=import_history_snapshot(db,path,'P',digest)
    assert result['status']=='already_imported'
    assert db.execute('SELECT count(*) FROM sales_matching_history').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM sales_matching_sources').fetchone()[0]==1
    assert before==list(db.execute('SELECT * FROM records'))


def test_invalid_hash_or_source_schema_does_not_publish(tmp_path):
    path,digest=snapshot(tmp_path,[historical()]);db=database()
    with pytest.raises(ValueError,match='hash_mismatch'):
        import_history_snapshot(db,path,'P','0'*64)
    assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'sales_matching_%'").fetchall()
    with sqlite3.connect(path) as source:
        source.execute('DROP TABLE historical_orders');source.execute('CREATE TABLE historical_orders(raw_remark TEXT)')
    with pytest.raises(ValueError,match='schema_incomplete'):
        import_history_snapshot(db,path,'P',file_sha256(path))


def test_failed_import_rolls_back_new_snapshot_and_leaves_active_source(tmp_path):
    db,path,digest,_=prepared(tmp_path)
    second=tmp_path/'second.sqlite'
    path.replace(second)
    path,new_hash=snapshot(tmp_path,[historical('duplicate'),historical('duplicate',code='LS2')])
    with pytest.raises(sqlite3.IntegrityError):
        import_history_snapshot(db,path,'P',new_hash)
    assert db.execute('SELECT count(*) FROM sales_matching_history').fetchone()[0]==1
    assert db.execute('SELECT source_sha256 FROM sales_matching_sources WHERE active=1').fetchone()[0]==digest


def test_cli_only_accepts_new_marked_isolated_runtime(tmp_path):
    target=tmp_path/'sales-history-isolated-test'
    assert cli.prepare_runtime(target)['isolated']
    path,digest=snapshot(tmp_path,[historical()])
    result=cli.import_to_isolated_runtime(target,path,'P',digest)
    assert result['eligible_rows']==1
    with pytest.raises(ValueError,match='must_be_new'):cli.prepare_runtime(target)
    with pytest.raises(ValueError,match='name_required'):cli.prepare_runtime(tmp_path/'data')
    with pytest.raises(ValueError,match='marker_required'):
        cli.import_to_isolated_runtime(tmp_path/'sales-history-isolated-unmarked',path,'P',digest)
    with pytest.raises(ValueError,match='production_or_fixture'):
        cli.validate_runtime('/opt/ul-workbench/service/runtime/sales-history-isolated-test',preparing=True)


def test_cli_rejects_symlink_and_other_tables(tmp_path):
    real=tmp_path/'sales-history-isolated-real';cli.prepare_runtime(real)
    alias=tmp_path/'sales-history-isolated-alias';alias.symlink_to(real,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):cli.validate_runtime(alias)
    with sqlite3.connect(real/cli.DATABASE) as db:db.execute('CREATE TABLE staff_users(secret TEXT)')
    path,digest=snapshot(tmp_path,[historical()])
    with pytest.raises(ValueError,match='other_tables'):
        cli.import_to_isolated_runtime(real,path,'P',digest)
