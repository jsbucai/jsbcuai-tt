import json
import uuid
import pytest
import xlrd
from core.adjustment_web import WebAdjustmentJobs
from core.adjustments import AdjustmentConflict
from core.adjustment_web_import import prepare_workbook, submit_once
from core.erp_adjustments import AdjustmentUnknown
from core.sql_queries import Queries
from core.store import dumps
from tests.test_adjustments import services, payload
from tests.test_sales_web_import import config
from tools.erp_human_challenge import Challenge
from tools.weixin_erp_verification import ingest

ACTOR={'id':'packer','display_name':'包装员'}
BINDING={'enabled':True,'profile_id':'P','account_id':'A','warehouse_id':'W','warehouse_name':'江苏优螺','maker_name':'罗健树'}


def setup(services):
    store,_=services
    jobs=WebAdjustmentJobs(store,Queries(store))
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('adjustment_web_config',?)",(dumps(BINDING),))
        item={'sku':'SKU001','profile_id':'P','warehouse_id':'W','product_id':'p1','sku_id':'s1','base_unit':'u1'}
        db.execute("UPDATE records SET payload=? WHERE kind='inventory' AND sku='SKU001'",(dumps(item),))
    return jobs


def test_intake_scope_actor_replay_and_snapshot_freeze(services):
    jobs=setup(services);p=payload()
    first=jobs.enqueue(p,ACTOR)
    assert first['status']=='queued'
    assert jobs.enqueue(p,ACTOR)==first
    with pytest.raises(AdjustmentConflict):jobs.enqueue(p,{'id':'other'})
    with pytest.raises(AdjustmentConflict):jobs.authorize(p['id'],{'id':'other'})
    p2=payload();p2['items'][0]['warehouse_id']='different'
    with pytest.raises(AdjustmentConflict):jobs.enqueue(p2,ACTOR)
    with jobs.store.connect() as db:
        frozen=json.loads(db.execute('SELECT binding FROM adjustment_web_bindings').fetchone()[0])
    assert frozen['items'][0]['sku_id']=='s1'


class Browser:
    clicks=0
    def account_identity(self):return ('P','A')
    def stage_adjustment(self,path,kind): self.kind=kind
    def click_upload_once(self):self.clicks+=1
    def visible_import_progress(self):return '业务单据导入进度\n1,开始导入\n2,开始检查格式\n3,本次导入1行数据,开始写入数据\n4,-----完成-----\n'


@pytest.mark.parametrize('kind',['loss','overflow'])
def test_linux_xls_roundtrip_and_once_only_receipt(services,config,kind):
    jobs=setup(services);batch=jobs.enqueue(payload(kind),ACTOR)
    path,number=prepare_workbook(jobs.store.runtime,config.template_path,batch,BINDING)
    sheet=xlrd.open_workbook(str(path)).sheet_by_index(0)
    assert sheet.cell_value(1,15)=='SKU001' and sheet.cell_value(1,19)==2.5
    browser=Browser()
    result=submit_once(path,number,batch,browser,BINDING)
    assert result['document_no']==number and browser.clicks==1
    submit_once(path,number,batch,browser,BINDING)
    assert browser.clicks==1


def test_uncertain_never_uploads_twice(services,config):
    jobs=setup(services);batch=jobs.enqueue(payload(),ACTOR)
    path,number=prepare_workbook(jobs.store.runtime,config.template_path,batch,BINDING)
    browser=Browser();browser.visible_import_progress=lambda:'partial'
    with pytest.raises(AdjustmentUnknown):submit_once(path,number,batch,browser,BINDING)
    with pytest.raises(AdjustmentUnknown):submit_once(path,number,batch,browser,BINDING)
    assert browser.clicks==1


def test_verification_one_use_expiry_and_not_sales(tmp_path):
    root=tmp_path/'human';c=Challenge(root)
    q=c.create(str(uuid.uuid4()),'code');calls=[]
    m={'message_id':'one','item_list':[{'type':1,'text_item':{'text':f"验证码 {q['id']} 123456"}}]}
    ingest(m,calls.append,root)
    assert calls==[] and c.answer(q['id'])=='123456' and c.answer(q['id']) is None
    c.close(q['id'],'expired');ingest(m,calls.append,root)
    assert c.answer(q['id']) is None and not calls
    assert '123456' not in (root/'request.json').read_text()

from tests.test_staff_auth import public_app, admin_session

def test_public_api_requires_auth_and_uses_n150_queue(public_app):
    store=public_app.extensions['service']['store'];client=public_app.test_client()
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('adjustment_web_config',?)",(dumps(BINDING),))
        db.execute('INSERT INTO records(kind,id,payload,sku) VALUES (?,?,?,?)',
          ('inventory','item',dumps({'sku':'SKU001','profile_id':'P','warehouse_id':'W','product_id':'p','sku_id':'s','base_unit':'u'}),'SKU001'))
    value=payload()
    assert client.post('/api/v2/adjustment-batches',json=value).status_code==401
    headers=admin_session(client)
    result=client.post('/api/v2/adjustment-batches',headers=headers,json=value)
    assert result.status_code==200, result.get_json()
    assert result.get_json()['batch']['status']=='queued'
    assert client.get('/api/v2/adjustment-batches/'+value['id'],query_string={'client_id':value['client_id']},headers=headers).status_code==200

def test_two_staff_concurrent_claim_same_id_stays_single_owner(services):
    from concurrent.futures import ThreadPoolExecutor
    jobs=setup(services);p=payload()
    def create(actor):
        try:return jobs.enqueue(p,{'id':actor})['status']
        except AdjustmentConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(create,['a','b']))
    assert sorted(results)==['conflict','queued']


def test_collector_503_is_not_verification():
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
    from purchase_export_collector.collector import _login_failure,CollectorError
    class Frame:
        url='https://bfuned5main.wsgjp.com.cn/UserLogin.gspx'
        def locator(self,_):return self
        def inner_text(self,**kw):return 'HTTP Error 503. Service Unavailable'
    from types import SimpleNamespace as NS
    p=NS(context=NS(pages=[NS(frames=[Frame()])]))
    with pytest.raises(CollectorError,match='erp_service_unavailable'):_login_failure(p)


def test_adjustment_waits_for_delayed_form_without_reclicking():
    from types import SimpleNamespace
    from core.adjustment_web_import import AdjustmentBrowser
    from core.sales_web_transport import PlaywrightUploadSelectors
    class Control:
        ready = False
        def wait_for(self, **kwargs):
            assert kwargs == {'state': 'visible', 'timeout': 15000}
            self.ready = True
        def count(self): return 1 if self.ready else 0
        def is_visible(self): return self.ready
    control = Control()
    browser = AdjustmentBrowser(SimpleNamespace(locator=lambda _: control),
        PlaywrightUploadSelectors(), identity=lambda: ('P', 'A'))
    assert browser._ready_control('input') is control


def test_adjustment_wait_keeps_ambiguous_controls_blocked():
    from types import SimpleNamespace
    from core.adjustment_web_import import AdjustmentBrowser
    from core.sales_web_transport import PlaywrightUploadSelectors
    control = SimpleNamespace(wait_for=lambda **_: None, count=lambda: 2,
                              is_visible=lambda: True)
    browser = AdjustmentBrowser(SimpleNamespace(locator=lambda _: control),
        PlaywrightUploadSelectors(), identity=lambda: ('P', 'A'))
    with pytest.raises(RuntimeError, match='ambiguous'):
        browser._ready_control('input')


def test_adjustment_form_timeout_never_marks_an_upload(services, config):
    from core.adjustments import AdjustmentFailure
    jobs = setup(services);batch = jobs.enqueue(payload(), ACTOR)
    path, number = prepare_workbook(jobs.store.runtime, config.template_path, batch, BINDING)
    browser = Browser()
    def timeout(*_): raise TimeoutError('form not ready')
    browser.stage_adjustment = timeout
    with pytest.raises(AdjustmentFailure, match='导入前核对'):
        submit_once(path, number, batch, browser, BINDING)
    assert browser.clicks == 0
    assert not (path.parent/'submission.json').exists()
    diagnostic=json.loads((path.parent/'diagnostic.json').read_text())
    assert diagnostic['phase']=='before_upload' and diagnostic['error_type']=='TimeoutError'


def test_adjustment_diagnostic_cannot_enable_replay(services, config):
    jobs=setup(services);batch=jobs.enqueue(payload(),ACTOR)
    path,number=prepare_workbook(jobs.store.runtime,config.template_path,batch,BINDING)
    browser=Browser();browser.visible_import_progress=lambda:'partial'
    def broken(): raise RuntimeError('private-token-never-save')
    browser.diagnostic=broken
    with pytest.raises(AdjustmentUnknown):submit_once(path,number,batch,browser,BINDING)
    assert (path.parent/'submission.json').exists()
    saved=(path.parent/'diagnostic.json').read_text()
    assert 'after_attempt' in saved and 'private-token' not in saved
    with pytest.raises(AdjustmentUnknown):submit_once(path,number,batch,browser,BINDING)
    assert browser.clicks==1


def test_adjustment_does_not_disguise_import_failures_as_login():
    from core.adjustments import AdjustmentFailure
    from tools.adjustment_web_worker import pre_upload_failure
    original = AdjustmentFailure('导入前核对未通过，尚未提交 ERP', retryable=True)
    assert pre_upload_failure(original) is original
    assert '密码' in str(pre_upload_failure(RuntimeError('login_password_rejected')))
    assert '503' in str(pre_upload_failure(RuntimeError('erp_service_unavailable')))
    assert 'secret' not in str(pre_upload_failure(RuntimeError('secret query URL')))


@pytest.mark.parametrize('confirmation_timeout',[False,True])
def test_adjustment_confirmation_wait_never_reclicks(confirmation_timeout):
    from types import SimpleNamespace
    from core.adjustment_web_import import AdjustmentBrowser
    from core.sales_web_transport import PlaywrightUploadSelectors
    events=[]
    def wait_for(**kw):
        assert kw=={'state':'visible','timeout':30000}
        events.append('wait')
        if confirmation_timeout:raise TimeoutError('slow ERP')
    page=SimpleNamespace(get_by_text=lambda *a,**kw:SimpleNamespace(wait_for=wait_for))
    browser=AdjustmentBrowser(page,PlaywrightUploadSelectors(),identity=lambda:('P','A'))
    browser._session=lambda:None
    browser._ready_control=lambda _:SimpleNamespace(click=lambda **kw:events.append('upload'))
    browser._visible_text=lambda _:SimpleNamespace(click=lambda **kw:events.append('confirm'))
    if confirmation_timeout:
        with pytest.raises(TimeoutError):browser.click_upload_once()
        assert events==['upload','wait']
    else:
        browser.click_upload_once()
        assert events==['upload','wait','confirm']

def test_captcha_image_encrypted_for_bound_owner_and_cdn_no_credentials(tmp_path,monkeypatch):
    import base64
    from tools import weixin_erp_verification as v
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    path=tmp_path/'captcha.png';raw=b'\x89PNG'+b'test-image';path.write_bytes(raw)
    seen={}
    def request(base,endpoint,body,token):
        assert token=='private-bot-token' and body['to_user_id']=='bound-owner'
        seen.update(body)
        return {'upload_full_url':'https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=x'}
    class Response:
        status=200;headers={'x-encrypted-param':'download'}
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        def open(self,req,timeout):
            assert not req.has_header('Authorization')
            cipher=Cipher(algorithms.AES(bytes.fromhex(seen['aeskey'])),modes.ECB()).decryptor()
            value=cipher.update(req.data)+cipher.finalize();unpad=padding.PKCS7(128).unpadder()
            assert unpad.update(value)+unpad.finalize()==raw
            return Response()
    monkeypatch.setattr(v.urllib.request,'build_opener',lambda *a:Opener())
    result=v.upload_image({'base':'https://ilinkai.weixin.qq.com','bot_token':'private-bot-token','ilink_user_id':'bound-owner'},path,request,lambda:object())
    assert result['type']==2
    assert base64.b64decode(result['image_item']['media']['aes_key']).decode()==seen['aeskey']
