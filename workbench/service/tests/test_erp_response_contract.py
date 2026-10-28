"""Only the documented Qimen read method accepts its alternate envelope."""
import io
import json
from pathlib import Path
import sys
import time

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import wsgjp_gateway as gateway
from core.erp_api import ErpClient, ErpUnavailable


GOOD={'code':'0','TotalResults':2,'totalResults':1,'trade_orders':{'trade_orders':[{'trade_order':{'trade_id':'example'},'order_info':{'order_info':[]}}]}}


@pytest.mark.parametrize('envelope',[GOOD,{'iserror':False,'response':GOOD},
    {'iserror':False,'response':json.dumps(GOOD)}])
def test_qimen_direct_and_standard_wrapped_success_are_preserved(envelope):
    assert gateway.unwrap_response(envelope,gateway.QIMEN_ORDER_QUERY)==GOOD


@pytest.mark.parametrize('method',[None,'erp.ewms.stockoutbill.query','beefun.orderfreight.updatefreight'])
def test_alternate_success_shape_is_never_accepted_by_other_methods(method):
    with pytest.raises(gateway.ApiError):gateway.unwrap_response(GOOD,method)
    assert gateway.unwrap_response({'iserror':False,'response':{'orders':[]}},method)=={'orders':[]}


@pytest.mark.parametrize('envelope',[
    {'code':False},{'code':None},{'trade_orders':[]},
    {'iserror':False,'response':'malformed'},
    {'iserror':True,'response':GOOD},
    {'iserror':False,'response':{'code':'1','error_info':'签名错误 secret-token','error_code':'10'}},
])
def test_qimen_success_requires_both_outer_and_inner_success(envelope):
    with pytest.raises(gateway.ApiError) as caught:
        gateway.unwrap_response(envelope,gateway.QIMEN_ORDER_QUERY)
    assert 'secret-token' not in str(caught.value)


def test_vendor_error_keeps_only_safe_category_and_numeric_code():
    error=gateway.vendor_error('签名错误 token=secret', '10')
    assert error.category=='signature' and error.code==10 and str(error)=='signature'
    assert gateway.vendor_error('unexpected secret payload','secret').code is None
    assert gateway.vendor_error('unexpected',False).code is None


def test_transport_selects_alternate_parser_only_by_requested_method(monkeypatch):
    class Opener:
        def open(self,request,timeout):return io.BytesIO(json.dumps(GOOD).encode())
    monkeypatch.setattr(gateway.urllib.request,'build_opener',lambda *args:Opener())
    assert gateway.post('/allnewapi',{'method':gateway.QIMEN_ORDER_QUERY})==GOOD
    with pytest.raises(gateway.ApiError):gateway.post('/api/token',{'method':gateway.QIMEN_ORDER_QUERY})
    with pytest.raises(gateway.ApiError):gateway.post('/allnewapi',{'method':'erp.stock.list'})


def test_service_allowlist_accepts_qimen_read_and_preserves_safe_error_metadata(tmp_path):
    path=tmp_path/'vault.json'
    path.write_text(json.dumps({'credentials':{'appkey':'1'*32,'appsecret':'A'*32,'signkey':'sign'},
        'token':{'appkey':'1'*32,'profileid':'42','auth_token':'secret','expires_at':time.time()+3600}}))
    path.chmod(0o600)
    calls=[]
    def transport(endpoint,payload):
        calls.append((endpoint,payload['method']))
        return GOOD
    client=ErpClient(path,transport=transport)
    assert client.call(gateway.QIMEN_ORDER_QUERY,{'pageNo':1,'pageSize':100})==GOOD
    assert calls==[('/allnewapi',gateway.QIMEN_ORDER_QUERY)]
    with pytest.raises(ErpUnavailable):client.call('beefun.deliver.updatefreightinfo',{})
    def failed(*args):raise gateway.ApiError('signature',10)
    client.transport=failed
    with pytest.raises(ErpUnavailable) as caught:client.call(gateway.QIMEN_ORDER_QUERY,{})
    assert caught.value.category=='signature' and caught.value.code==10
    assert 'secret' not in str(caught.value)
