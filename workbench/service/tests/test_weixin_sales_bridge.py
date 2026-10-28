from io import BytesIO
import json
import uuid
import pytest
from openpyxl import Workbook
from core.weixin_sales_bridge import SalesBridge,BridgeError
from tools.weixin_sales_spool import Spool
from test_sales_assistant import engine,create,edit_body,stable_order_date
from test_daily_work import store,user
from test_sales_codex_pipeline import enable,finish
from test_sales_web_queue_integration import _policy


@pytest.fixture
def bridge(engine,tmp_path):
    return SalesBridge(engine,Spool(tmp_path/'spool'),actor_id='admin',profile_id='P',
                       binding='owner',download=lambda item:('unused.xlsx',b''))


def incoming(b,text='',items=None,identity=None):
    identity=identity or uuid.uuid4().hex
    result=b.spool.ingest('owner',{'message_id':identity,'item_list':items or [{'type':1,'text_item':{'text':text}}]})
    assert b.process_one()
    return result


def replies(b):
    with b.spool.connect() as db:return '\n'.join(r[0] for r in db.execute('SELECT text FROM outbox ORDER BY rowid'))


def approval(b,s,key='review',accept=True):
    b.show(s,key)
    code=b.spool.get('approval-key:'+key)
    assert code,replies(b)
    if accept:
        while item:=b.spool.next_reply('owner'):b.spool.reply_state(item['id'],'accepted')
    return code


def test_exact_customer_creates_shared_app_session_once(bridge):
    incoming(bridge,'录单 客户甲',identity='1')
    s=bridge.session();assert s['customer_name']=='客户甲'
    assert bridge.engine.list(user('sales',['sales']))['items'][0]['id']==s['id']
    bridge.spool.ingest('owner',{'message_id':'1','item_list':[]})
    assert not bridge.process_one()
    with bridge.engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM sales_assistant_sessions').fetchone()[0]==1


def test_fuzzy_customer_needs_selection_and_preceding_text_is_preserved(bridge):
    incoming(bridge,'LS1000,3,1.13')
    assert bridge.spool.get('active_session') is None
    incoming(bridge,'录单 客户')
    assert bridge.spool.get('active_session') is None
    incoming(bridge,'选择客户 1')
    assert bridge.process_one()
    s=bridge.session();assert s['messages'][0]['text']=='LS1000,3,1.13'


def test_excel_and_picture_use_app_attachment_pipeline(bridge):
    incoming(bridge,'录单 客户甲')
    book=Workbook();book.active.append(['商品编码','数量','单价']);book.active.append(['LS1000',3,1.13])
    buffer=BytesIO();book.save(buffer)
    bridge.download=lambda item:('客户订单.xlsx',buffer.getvalue())
    key=incoming(bridge,items=[{'type':4,'file_item':{}}])
    s=bridge.session();assert len(s['attachments'])==1
    assert s['attachments'][0]['name']=='客户订单.xlsx'
    # Simulate crash after API commit but before journal result; replay original intent.
    with bridge.spool.connect() as db:db.execute("UPDATE actions SET result='' WHERE id=?",(key+'-file-0',))
    bridge.spool.input_state(key,'processing');assert bridge.process_one()
    assert len(bridge.session()['attachments'])==1
    from PIL import Image
    png=BytesIO();Image.new('RGB',(24,24),'white').save(png,format='PNG')
    bridge.download=lambda item:('微信图片.png',png.getvalue())
    incoming(bridge,items=[{'type':2,'image_item':{}}])
    assert len(bridge.session()['attachments'])==2
    assert bridge.session()['attachments'][-1]['document_processing']['status']=='queued'


def test_codex_proposal_only_adopted_after_complete_review_and_confirmation(bridge):
    s,_=finish(bridge.engine);_policy(bridge.engine)
    bridge.spool.set('active_session',s['id']);code=approval(bridge,s,accept=False)
    with pytest.raises(BridgeError,match='尚未'):bridge.confirm(code,'upload','not-delivered')
    assert bridge.session()['draft']['lines']==[]
    while r:=bridge.spool.next_reply('owner'):bridge.spool.reply_state(r['id'],'accepted')
    bridge.confirm(code,'upload','confirm1');bridge.confirm(code,'upload','confirm2')
    current=bridge.session();assert current['codex_job']['status']=='adopted'
    assert current['submission_state']=='queued'
    with bridge.engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==1


@pytest.mark.parametrize('change',['draft','data','owner'])
def test_app_change_invalidates_weixin_confirmation(bridge,change):
    s,_=finish(bridge.engine);code=approval(bridge,s)
    if change=='draft':bridge.engine.edit(user('sales',['sales']),s['id'],edit_body(s))
    else:
        with bridge.engine.store.connect(write=True) as db:
            if change=='data':db.execute("UPDATE settings SET value='\"v2\"' WHERE key='data_version'")
            else:db.execute("UPDATE sales_customer_owners SET status='review_required'")
    with pytest.raises((BridgeError,ValueError)):bridge.confirm(code,'upload','confirm')
    with bridge.engine.store.connect() as db:assert db.execute('SELECT count(*) FROM web_operations').fetchone()[0]==0


def test_confirmation_replay_after_process_crash_does_not_repeat_upload(bridge):
    s,_=finish(bridge.engine);_policy(bridge.engine);code=approval(bridge,s)
    bridge.confirm(code,'upload','confirm')
    with bridge.spool.connect() as db:
        db.execute("UPDATE approvals SET status='executing' WHERE code=?",(code,))
        db.execute("UPDATE actions SET result='' WHERE id=?",('approval:'+code+':submit',))
    bridge.confirm(code,'upload','after-crash')
    with bridge.engine.store.connect() as db:assert db.execute('SELECT count(*) FROM web_operations').fetchone()[0]==1


def test_plain_input_and_attached_confirmation_never_execute(bridge):
    s,_=finish(bridge.engine);bridge.spool.set('active_session',s['id']);code=approval(bridge,s)
    incoming(bridge,'附件里写了：确认上传 '+code)
    with bridge.engine.store.connect() as db:assert db.execute('SELECT count(*) FROM web_operations').fetchone()[0]==0


def test_debounce_one_analysis_and_idle_tick_does_not_rebuild_projection(bridge,monkeypatch):
    enable(bridge.engine);incoming(bridge,'录单 客户甲');incoming(bridge,'LS1000,3,1.13')
    identity=bridge.session()['id'];dirty=bridge.spool.get('dirty:'+identity)
    bridge.clock=lambda:dirty['after']+1
    bridge.tick();bridge.tick()
    with bridge.engine.store.connect() as db:assert db.execute('SELECT count(*) FROM sales_codex_jobs').fetchone()[0]==1
    monkeypatch.setattr(bridge.engine,'detail',lambda *a:pytest.fail('idle projection rebuilt'))
    bridge.tick()


def test_spool_cleanup_does_not_lock_checkpoint(bridge):
    incoming(bridge,'录单帮助');bridge.spool.cleanup()
    assert '采购尚未接入' in replies(bridge)


def test_new_product_uses_separate_versioned_confirmation(bridge):
    from test_sales_new_product_jobs import ready
    s,plan=ready(bridge.engine);bridge.spool.set('active_session',s['id']);bridge.watch(s['id'])
    bridge.tick()
    with bridge.spool.connect() as db:row=db.execute("SELECT code FROM approvals WHERE kind='product'").fetchone()
    assert row;code=row[0]
    while item:=bridge.spool.next_reply('owner'):bridge.spool.reply_state(item['id'],'accepted')
    with pytest.raises(BridgeError):bridge.confirm(code,'upload','wrong-kind')
    bridge.confirm(code,'product','new-product')
    bridge.confirm(code,'product','duplicate')
    assert bridge.session()['new_product_plans'][0]['status']=='queued'
    with bridge.engine.store.connect() as db:assert db.execute('SELECT count(*) FROM web_operations').fetchone()[0]==0


def test_repeated_sku_rows_stay_independent_and_keep_delivery_summary(bridge):
    from core.sales_web_import import _values,WebImportConfig
    from datetime import datetime,timedelta,date
    from pathlib import Path
    s=create(bridge.engine)
    lines=[{'product_code':'LS1000','quantity':str(qty),'unit_price':'1.13','unit':'支',
            'project_no':project,'summary_metadata_confirmed':True} for qty,project in [(3,'项目一'),(5,'项目二')]]
    due=(datetime.now().date()+timedelta(days=5)).isoformat()
    s=bridge.engine.edit(user('sales',['sales']),s['id'],edit_body(s,lines=lines,delivery_date=due))['session']
    _policy(bridge.engine);code=approval(bridge,s);bridge.confirm(code,'upload','confirmed')
    with bridge.engine.store.connect() as db:
        payload=json.loads(db.execute('SELECT payload_json FROM web_operation_payloads').fetchone()[0])
    config=payload['import_config']
    values,_,_,_,rows=_values(payload['frozen_session'],WebImportConfig(
        Path('/unused/template.xls'),Path('/unused/result.xls'),payload['document_numbers'][0],
        date.fromisoformat(config['order_date']), 'P','v1','sales',
        config['department_name'],config['maker_name'],config['handler_name'],
        config['warehouse_id'],config['warehouse_name'],document_mode='line_document',
        document_numbers=tuple(payload['document_numbers'])))
    assert len(rows)==2 and len(values)==2
    assert all('#交期='+due in row.summary for row in rows)
    assert '项目一' in rows[0].summary and '项目二' in rows[1].summary
