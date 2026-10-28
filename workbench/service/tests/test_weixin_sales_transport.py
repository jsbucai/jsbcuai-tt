from datetime import timedelta
import pytest
from tools import weixin_guardian as w
from tools.weixin_sales_spool import Spool
from test_weixin_guardian import AT,account,msg


def test_spool_failure_never_acknowledges_message_or_cursor():
    state={'cursor':'old'}
    def fail(message):raise OSError('disk full')
    with pytest.raises(OSError):w.receive(account(),state,{'msgs':[msg(text='录单 客户甲')],'get_updates_buf':'new'},AT,fail)
    assert state['cursor']=='old' and not state['seen_messages'] and 'context_token' not in state


def test_only_bound_owner_enters_journal_and_status_does_not(tmp_path):
    spool=Spool(tmp_path/'spool');state={}
    ingest=lambda message:spool.ingest('bound',message)
    for message in (msg(sender='other',text='录单 客户甲'),msg(group='g',text='录单 客户甲'),msg(text='状态')):
        w.receive(account(),state,{'msgs':[message]},AT,ingest)
    assert spool.next_input('bound') is None
    message=msg(text='录单 客户甲');message['message_id']=456
    w.receive(account(),state,{'msgs':[message]},AT,ingest)
    assert spool.next_input('bound')


def test_business_message_budget_and_snapshot_are_independent(tmp_path,monkeypatch):
    state={'context_token':'x','budget_day':AT.date().isoformat(),'budget_used':10,
           'accepted_snapshot':{'keep':True}}
    monkeypatch.setattr(w,'request',lambda *a,**kw:{})
    event={'id':'sales-1','kind':'sales','text':'草稿','target':{}}
    assert w.send(account(),state,event,AT,tmp_path/'state.json')['status']=='accepted'
    assert state['budget_used']==10 and state['sales_budget_used']==1
    assert state['accepted_snapshot']=={'keep':True} and 'initial_test_accepted' not in state


def test_review_final_code_waits_for_all_pages_and_unknown_stops_chain(tmp_path,monkeypatch):
    spool=Spool(tmp_path/'spool');state={'context_token':'x'}
    spool.reply('page1','bound','第一行');spool.reply('page2','bound','第二行','page1')
    spool.reply('code','bound','确认上传 AABBCCDD','page2')
    sent=[]
    def fail(*a,**kw):sent.append(1);raise TimeoutError()
    monkeypatch.setattr(w,'request',fail)
    w.deliver_sales((spool,'bound'),account(),state,AT,tmp_path/'state.json')
    assert len(sent)==1 and not spool.delivered('code')
    assert spool.next_reply('bound') is None


def test_sales_lifetime_is_independent_from_three_day_observation(tmp_path,monkeypatch):
    config=tmp_path/'config.json';monkeypatch.setattr(w,'SALES_CONFIG',config)
    assert not w.active_window(w.STOP+timedelta(days=1))
    w.save(config,{'enabled':True})
    assert w.active_window(w.STOP+timedelta(days=1))
