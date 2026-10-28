"""Evidence precedence and sync-safe continuation, without ERP writes."""
import json,uuid
from io import BytesIO
from openpyxl import Workbook
from core.store import dumps
from core.sales_source_parser import parse_excel
from core.sales_dependencies import product_dependencies
from test_daily_work import store,user
from test_sales_assistant import engine
from test_sales_codex_pipeline import request,finish,output
from test_sales_skill_matching import database,product,source,context,historical,history_reference,rules


def test_customer_po_headers_preserve_physical_rows_and_untaxed_values():
    book=Workbook();sheet=book.active
    sheet.append(['订单号','物料','中文描述','下单日期','计划交货日期','采购数量','未税单价（元/件）','供应商','备注'])
    sheet.append(['PO1','000123','内六角圆柱头螺钉M16X45-8.8','2026-10-01','2026-10-21',500,.717,'供应商',''])
    sheet.append(['PO1','000123','内六角圆柱头螺钉M16X45-8.8','2026-10-01','2026-10-22',200,.6643,'供应商',''])
    raw=BytesIO();book.save(raw)
    lines,reports=parse_excel(raw.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','A','hash','客户甲')
    assert len(lines)==2 and reports[0]['status']=='ignored_with_reason'
    assert [r['quantity'] for r in lines]==['500','200']
    assert [r['unit_price'] for r in lines]==['0.717','0.6643']
    assert lines[0]['tax_basis']=='tax_exclusive'
    assert lines[0]['source_ref']['customer_part_no']=='000123' and not lines[0]['product_code']
    assert lines[0]['delivery_date']=='2026-10-21'
    assert lines[0]['source_ref']['id']!=lines[1]['source_ref']['id']


def test_unique_exact_part_history_beats_generic_finish_alternatives(tmp_path,rules):
    db=database();product(db);product(db,code='LS2',record='R2',model='8.8级 蓝白锌')
    history=history_reference(tmp_path,[historical()])
    result=context(db,[source(customer_part_no='PART-1',original_surface='')],history_db=history,rules=rules)['lines'][0]
    assert result['available_candidate_count']==2
    assert result['suggested_product_code']=='LS1' and not result['needs_question']


def test_two_reliable_part_mappings_still_require_choice(tmp_path,rules):
    db=database();product(db);product(db,code='LS2',record='R2',model='8.8级 蓝白锌')
    history=history_reference(tmp_path,[historical(),historical(identity='2',code='LS2',customer_surface='蓝白锌')])
    result=context(db,[source(customer_part_no='PART-1',original_surface='')],history_db=history,rules=rules)['lines'][0]
    assert result['needs_question'] and not result['suggested_product_code']


def test_standard_alias_and_small_round_nut_pitch_are_scoped(rules):
    db=database();product(db,code='LD1',product_name='内六角圆柱头螺钉GB70.1,DIN912',standard_no='',spec='M16*45')
    r=context(db,[source(original_name='内六角圆柱头螺钉',original_spec='M16X45',original_standard='GB/T70.1')],rules=rules)['lines'][0]
    assert r['suggested_product_code']=='LD1'
    product(db,code='LM1',record='R2',product_name='小圆螺母GB/T810',standard_no='',spec='M85-2',model='发黑')
    product(db,code='WRONG',record='R3',product_name='滚动轴承锁紧螺母KMT17',standard_no='',spec='M85*2 D1=110',model='发黑')
    r=context(db,[source(original_name='小圆螺母',original_spec='M85X2',original_standard='GB/T810',original_grade='')],rules=rules)['lines'][0]
    assert r['suggested_product_code']=='LM1'
    assert all(c['product_code']!='WRONG' for c in r['candidates'])


def test_stock_record_ids_and_unrelated_catalog_changes_keep_proposal_adoptable(engine):
    session,_=finish(engine);job=session['codex_job']
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE records SET id='new-api-row',payload=json_set(payload,'$.available_stock',0,'$.updated_at','tomorrow') WHERE kind='inventory'")
        db.execute("INSERT INTO records(kind,id,sku,payload) VALUES ('inventory','unrelated','UNRELATED',?)",(dumps({'profile_id':'P','product_name':'new product'}),))
        db.execute("UPDATE settings SET value=? WHERE key='data_version'",(dumps('v2'),))
    current=engine.detail(user('sales',['sales']),session['id'])['session']
    assert current['codex_job']['can_adopt']
    adopted=engine.adopt_codex(user('sales',['sales']),session['id'],job['id'],{'version':session['version'],'result_hash':job['result_hash'],'idempotency_key':'adopt-refresh'})['session']
    assert len(adopted['draft']['lines'])==1 and not adopted['submission_operation_id']


def test_used_product_change_reports_code_and_retains_preview(engine):
    session,_=finish(engine)
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE records SET payload=json_set(payload,'$.sku_id','another-sku') WHERE kind='inventory'")
    job=engine.detail(user('sales',['sales']),session['id'])['session']['codex_job']
    assert not job['can_adopt'] and job['changed_product_codes']==['LS1000'] and job['preview']['lines']


def test_race_after_last_matching_cannot_publish_stale_product(engine):
    session=request(engine)['session'];token=uuid.uuid4().hex;snapshot=engine.codex.claim(token)
    with engine.store.connect(write=True) as db:
        expected=product_dependencies(db,'P',['LS1000'])
        db.execute("UPDATE records SET payload=json_set(payload,'$.base_unit','2') WHERE kind='inventory'")
    source_id=snapshot['messages'][-1]['id']
    result=engine.codex.finish(snapshot['job_id'],token,output=output(source_id),source_ids=[source_id],catalog_dependencies=expected)
    assert result['status']=='stale'


def test_followup_keeps_previous_proposal_and_all_user_corrections(engine):
    session,_=finish(engine)
    requested=request(engine,session,key='followup',text='第1行改为发黑，其他保持不变')
    with engine.store.connect() as db:
        snapshot=json.loads(db.execute('SELECT input_json FROM sales_codex_jobs WHERE id=?',(requested['job_id'],)).fetchone()[0])
    assert snapshot['previous_proposal']['draft']['lines'][0]['quantity']=='3'
    assert snapshot['messages'][-1]['text']=='第1行改为发黑，其他保持不变'
    assert len([m for m in snapshot['messages'] if m['role']=='user'])==2
