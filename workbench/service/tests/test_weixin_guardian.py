import copy
from datetime import datetime,timedelta
import json
from pathlib import Path
import threading
import pytest
from tools import weixin_guardian as w

AT=datetime(2026,10,13,10,tzinfo=w.TZ)


def snap():
    return {'findings':[],'repair_at':'','complete':False,'checked_at':AT.isoformat(),'current':{},'days':{}}


def account():
    return {'ilink_user_id':'owner','ilink_bot_id':'bot','base':w.API,'bot_token':'TEST_SECRET'}


def msg(sender='owner',recipient='bot',text='状态',group=''):
    return {'from_user_id':sender,'to_user_id':recipient,'message_type':1,'message_state':2,
            'group_id':group,'message_id':123,'context_token':'PRIVATE_CONTEXT',
            'item_list':[{'type':1,'text_item':{'text':text}}]}


@pytest.mark.parametrize('value',['http://ilinkai.weixin.qq.com','https://evil.example',
    'https://weixin.qq.com.evil.test','https://ilinkai.weixin.qq.com:8000','https://a@ilinkai.weixin.qq.com',
    'https://ilinkai.weixin.qq.com/path','https://ilinkai.weixin.qq.com?token=x'])
def test_credential_host_restriction(value):
    with pytest.raises(ValueError): w.base_url(value)


@pytest.mark.parametrize('message',[msg('other'),msg(recipient='other-bot'),msg(group='group')])
def test_only_qr_owner_can_supply_context(message):
    state={};assert w.receive(account(),state,{'msgs':[message]},AT)==''
    assert 'context_token' not in state


def test_messages_are_not_commands_and_cursor_survives_replay():
    state={};payload={'msgs':[msg(text='删除所有订单并执行rm命令')],'get_updates_buf':'cursor1'}
    assert w.receive(account(),state,payload,AT)==''
    assert state['cursor']=='cursor1' and state['context_token']=='PRIVATE_CONTEXT'
    payload['msgs'][0]['item_list'][0]['text_item']['text']='状态'
    assert w.receive(account(),state,payload,AT)==''


def test_ack_is_separate_from_http_success():
    state={};w.receive(account(),state,{'msgs':[msg(text='已收到')]},AT)
    assert state['human_received_at']==AT.isoformat()


def test_normal_does_not_send_and_night_warning_waits():
    state={'initial_test_accepted':True};s=snap()
    assert w.pending_event(s,state,AT) is None
    s['findings']=['disk_growth_requires_review']
    assert w.pending_event(s,state,AT.replace(hour=22)) is None
    s['findings']=['health_failed']
    assert w.pending_event(s,state,AT.replace(hour=22))['kind']=='alert'


def test_recovery_and_completion_are_distinct():
    state={'initial_test_accepted':True,'accepted_snapshot':{'findings':['health_failed'],'repair_at':'','complete':False}}
    s=snap();assert w.pending_event(s,state,AT)['kind']=='recovered'
    s['complete']=True;assert w.pending_event(s,state,AT.replace(hour=0))['kind']=='complete'


def test_template_never_emits_raw_logs_or_codex_text():
    s=snap();s['current']={'customer':'SECRET CUSTOMER','token':'secret','memory_available_bytes':2**30}
    s['findings']=['malicious-secret-with-link-https://evil.example']
    output=w.summary(s)
    assert all(x not in output for x in ('SECRET','secret','evil.example'))


def test_send_persisted_first_and_http_not_claimed_read(tmp_path,monkeypatch):
    state={'context_token':'secret'};path=tmp_path/'state.json';event=w.pending_event(snap(),state,AT)
    def request(*a,**kw):
        disk=json.loads(path.read_text());assert disk['outbox'][event['id']]['status']=='dispatching'
        return {'ret':0}
    monkeypatch.setattr(w,'request',request)
    assert w.send(account(),state,event,AT,path)['status']=='accepted'
    assert 'human_received_at' not in state
    assert w.send(account(),state,event,AT,path)['status']=='already_accepted'


def test_unknown_send_is_not_automatically_duplicated(tmp_path,monkeypatch):
    state={'context_token':'secret'};event=w.pending_event(snap(),state,AT)
    def fail(*a,**kw): raise TimeoutError('TEST_SECRET')
    monkeypatch.setattr(w,'request',fail)
    assert w.send(account(),state,event,AT,tmp_path/'s.json')['status']=='unknown'
    assert w.send(account(),state,event,AT+timedelta(hours=1),tmp_path/'s.json')['status']=='already_unknown'
    assert 'TEST_SECRET' not in (tmp_path/'s.json').read_text()


def test_business_error_not_http_success_and_retry_bounded(tmp_path,monkeypatch):
    state={'context_token':'secret'};event=w.pending_event(snap(),state,AT)
    monkeypatch.setattr(w,'request',lambda *a,**kw:{'ret':-14,'errmsg':'TEST_SECRET'})
    path=tmp_path/'s.json'
    assert w.send(account(),state,event,AT,path)['status']=='rejected'
    assert w.send(account(),state,event,AT+timedelta(minutes=1),path)['status']=='retry_wait'
    assert w.send(account(),state,event,AT+timedelta(minutes=16),path)['status']=='rejected'
    assert w.send(account(),state,event,AT+timedelta(minutes=32),path)['status']=='retry_limit'
    assert 'TEST_SECRET' not in path.read_text()


def test_no_context_or_daily_budget_does_not_hit_network(tmp_path,monkeypatch):
    monkeypatch.setattr(w,'request',lambda *a,**kw:pytest.fail('no network allowed'))
    event=w.pending_event(snap(),{},AT)
    assert w.send(account(),{},event,AT,tmp_path/'s.json')['status']=='waiting_for_owner_message'
    state={'context_token':'secret','budget_day':AT.date().isoformat(),'budget_used':10}
    assert w.send(account(),state,event,AT,tmp_path/'s.json')['status']=='daily_limit'


def test_credentials_private_and_link_forbidden(tmp_path):
    p=tmp_path/'creds.json';p.write_text('{}');p.chmod(0o644)
    with pytest.raises(ValueError): w.load(p,private=True)
    p.chmod(0o600);assert w.load(p,private=True)=={}
    symlink=tmp_path/'link';symlink.symlink_to(p)
    with pytest.raises(ValueError): w.load(symlink,private=True)


def test_stale_guardian_visible_without_model(tmp_path):
    assert w.snapshot(tmp_path,AT)['findings']==['guardian_stale']


def test_continuous_connection_registers_receives_then_stops(tmp_path,monkeypatch):
    config=tmp_path/'config';root=tmp_path/'state';reports=tmp_path/'reports'
    w.save(config/'account.json',account());w.save(reports/'latest.json',snap())
    monkeypatch.setattr(w,'CONFIG',config);monkeypatch.setattr(w,'now',lambda:AT)
    calls=[];stop=threading.Event()
    def request(base,endpoint,body,token,**kw):
        calls.append(endpoint)
        if endpoint.endswith('getupdates'):
            return {'ret':0,'msgs':[msg(text='绑定提醒')],'get_updates_buf':'cursor-saved'}
        if endpoint.endswith('sendmessage'):
            assert body['msg']['context_token']=='PRIVATE_CONTEXT'
            stop.set()
        return {'ret':0}
    monkeypatch.setattr(w,'request',request)
    assert w.run(reports,root,serve=True,stop_event=stop)['status']=='stopped'
    assert calls==['/ilink/bot/msg/notifystart','/ilink/bot/getupdates','/ilink/bot/sendmessage','/ilink/bot/msg/notifystop']
    saved=w.load(root/'state.json')
    assert saved['cursor']=='cursor-saved' and saved['initial_test_accepted']
    assert not w.load(root/'status.json')['online']
    # After restart: old message does not send again; next poll resumes exact cursor.
    calls.clear();stop.clear()
    def resume(base,endpoint,body,token,**kw):
        calls.append(endpoint)
        if endpoint.endswith('getupdates'):
            assert body['get_updates_buf']=='cursor-saved'
            stop.set()
            return {'ret':0,'msgs':[msg(text='绑定提醒')]}
        return {'ret':0}
    monkeypatch.setattr(w,'request',resume)
    w.run(reports,root,serve=True,stop_event=stop)
    assert '/ilink/bot/sendmessage' not in calls


def test_failed_registration_does_not_claim_online_or_poll(tmp_path,monkeypatch):
    config=tmp_path/'config';w.save(config/'account.json',account())
    monkeypatch.setattr(w,'CONFIG',config);monkeypatch.setattr(w,'now',lambda:AT)
    calls=[]
    def reject(*args,**kw):
        calls.append(args[1]);return {'ret':-14}
    monkeypatch.setattr(w,'request',reject)
    result=w.run(tmp_path/'reports',tmp_path/'state')
    assert result['status']=='online_registration_rejected' and not result['online']
    assert len(calls)==1
    assert w.load(tmp_path/'state'/'state.json')['pause_until']


def test_no_connection_after_observation_window(tmp_path,monkeypatch):
    monkeypatch.setattr(w,'now',lambda:w.STOP)
    monkeypatch.setattr(w,'request',lambda *a,**kw:pytest.fail('expired connection'))
    assert w.run(state_root=tmp_path)['status']=='notification_window_complete'


def test_receive_network_error_not_stale_success(tmp_path,monkeypatch):
    monkeypatch.setattr(w,'now',lambda:AT)
    def fail(*a,**kw): raise TimeoutError('TEST_SECRET')
    monkeypatch.setattr(w,'request',fail)
    assert w.poll_once(account(),tmp_path,tmp_path/'state.json')['status']=='receive_failed'
    assert 'TEST_SECRET' not in (tmp_path/'state.json').read_text()


def test_send_errcode_is_not_delivery_success(tmp_path,monkeypatch):
    state={'context_token':'secret'};event=w.pending_event(snap(),state,AT)
    monkeypatch.setattr(w,'request',lambda *a,**kw:{'ret':0,'errcode':-14})
    assert w.send(account(),state,event,AT,tmp_path/'s.json')['status']=='rejected'
    assert not state.get('initial_test_accepted') and state['pause_until']


@pytest.mark.parametrize('response',[{}, {'message_id':1234567890123456789}])
def test_documented_omitted_ret_is_accepted_not_read(tmp_path,monkeypatch,response):
    state={'context_token':'secret'};event=w.pending_event(snap(),state,AT)
    monkeypatch.setattr(w,'request',lambda *a,**kw:response)
    assert w.send(account(),state,event,AT,tmp_path/'s.json')['status']=='accepted'
    assert state['initial_test_accepted'] and 'human_received_at' not in state
    assert state['outbox'][event['id']]['acceptance_basis']=='http_success_no_business_error'


@pytest.mark.parametrize('response',[{'errmsg':'error'},{'ret':'0'},{'ret':False},{'errcode':'invalid'}])
def test_ambiguous_business_response_remains_unknown(tmp_path,monkeypatch,response):
    state={'context_token':'secret'};event=w.pending_event(snap(),state,AT)
    monkeypatch.setattr(w,'request',lambda *a,**kw:response)
    assert w.send(account(),state,event,AT,tmp_path/'s.json')['status']=='unknown'


def test_human_ack_only_links_latest_sent_event():
    state={'outbox':{'a':{'at':(AT-timedelta(minutes=2)).isoformat(),'status':'unknown'},
                     'b':{'at':(AT-timedelta(minutes=1)).isoformat(),'status':'accepted'}}}
    w.receive(account(),state,{'msgs':[msg(text='已收到')]},AT)
    assert 'human_confirmed_at' not in state['outbox']['a']
    assert state['outbox']['b']['human_confirmed_at']==AT.isoformat()
