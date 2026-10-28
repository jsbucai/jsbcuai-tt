import json, os, sys, shlex
import pytest
from tools.sales_app_server import invoke,AppServerError
from core import sales_api_transport as transport


def fake_server(tmp_path,mode):
    script=tmp_path/'protocol.py'
    script.write_text('#!'+sys.executable+'\n'+'''import json,sys,time
def out(x):print(json.dumps(x),flush=True)
for raw in sys.stdin:
 e=json.loads(raw)
 if e.get('id')==1:out({'id':1,'result':{}})
 elif e.get('id')==2:out({'id':2,'result':{'thread':{'id':'local'}}})
 elif e.get('id')==3:
  MODE='''+repr(mode)+'''
  if MODE=='request':out({'id':99,'method':'exec/approval','params':{}})
  elif MODE=='tool':out({'method':'item/started','params':{'item':{'id':'x','type':'commandExecution'}}})
  elif MODE=='timeout':time.sleep(20)
  else:
   out({'method':'item/completed','params':{'item':{'id':'secret','type':'reasoning','text':'must not be returned'}}})
   if MODE=='ok':out({'method':'item/completed','params':{'item':{'id':'answer','type':'agentMessage','text':'{"ok":true}'}}})
   out({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
''')
    launcher=tmp_path/'codex'
    launcher.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(script))+' "$@"\n')
    launcher.chmod(0o700)
    return launcher


def test_private_stdio_returns_structured_answer_not_reasoning(tmp_path):
    result,stats=invoke(fake_server(tmp_path,'ok'),tmp_path,'fixture',{},[],{'HOME':str(tmp_path)},[],timeout=3)
    assert result=={'ok':True} and stats['tool_calls']==0
    assert 'must not' not in json.dumps([result,stats])


@pytest.mark.parametrize('mode,code',[('request','codex_tool_use_forbidden'),('tool','codex_tool_use_forbidden'),('empty','codex_structured_result_missing'),('timeout','codex_timed_out')])
def test_stdio_fails_closed(tmp_path,mode,code):
    with pytest.raises(AppServerError,match=code):
        invoke(fake_server(tmp_path,mode),tmp_path,'fixture',{},[],{'HOME':str(tmp_path)},[],timeout=.5 if mode=='timeout' else 3)


@pytest.mark.parametrize('payload,status',[({'iserror':False,'response':{'vchcode':123}},'accepted'),
 ({'iserror':False,'response':{'vchcode':0}},'unknown'),({'iserror':False,'response':{}},'unknown'),
 ({'iserror':True,'errorcode':999,'message':'低于进价'},'rejected')])
def test_transport_requires_meaningful_receipt(monkeypatch,payload,status):
    from io import BytesIO
    from threading import RLock
    from types import SimpleNamespace
    class Response(BytesIO):
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
    calls=[]
    def open_once(*a,**kw):calls.append(a);return Response(json.dumps(payload).encode())
    monkeypatch.setattr(transport,'signed',lambda *a:{'appkey':'fixture-key','token':'fixture-token','sign':'fixture-sign'})
    monkeypatch.setattr(transport.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=open_once))
    receipt=transport.submit_once(SimpleNamespace(lock=RLock()),{'number':'test'})
    assert receipt['status']==status and len(calls)==1
