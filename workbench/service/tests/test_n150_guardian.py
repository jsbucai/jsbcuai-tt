import copy
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import pytest

from tools import n150_guardian as g


def packet(now):
    row = {'checked_at': now.isoformat(), 'health_ok': True,
           'memory_available_bytes': 1400*g.MB, 'disk_free_bytes': 12000*g.MB,
           'files': {'workbench.sqlite3': 200*g.MB},
           'container': {'Status': 'running', 'StartedAt': 'same'},
           'source': {'data_source_updated_at': now.isoformat()}}
    old = copy.deepcopy(row); old['checked_at'] = (now-timedelta(minutes=5)).isoformat()
    return {'current': row, 'previous': old, 'safety': {'ok': True, 'busy': False},
            'units': {}, 'maintenance_hold': False}


def test_night_source_age_is_not_failure():
    now = datetime(2026, 10, 13, 22, tzinfo=g.TZ); p = packet(now)
    p['current']['source']['data_source_updated_at'] = '2026-10-13T18:00:00+08:00'
    assert g.issues(p, now) == []
    now = now.replace(hour=10); p['current']['checked_at'] = now.isoformat()
    p['current']['source']['data_source_updated_at'] = '2026-10-13T08:00:00+08:00'
    assert 'daytime_source_delayed' in g.issues(p, now)


def test_partial_success_is_not_failure():
    now = datetime(2026, 10, 13, 10, tzinfo=g.TZ); p = packet(now)
    p['current']['jobs'] = [{'status': 'partial', 'stage': 'complete'}]
    assert g.issues(p, now) == []


def test_two_distinct_fresh_samples_for_low_memory():
    now = datetime(2026, 10, 13, 10, tzinfo=g.TZ); p = packet(now)
    p['current']['memory_available_bytes'] = 100*g.MB
    assert 'sustained_low_memory' not in g.issues(p, now)
    p['previous']['memory_available_bytes'] = 100*g.MB
    assert 'sustained_low_memory' in g.issues(p, now)
    p['previous']['checked_at'] = (now-timedelta(hours=2)).isoformat()
    assert 'sustained_low_memory' not in g.issues(p, now)


@pytest.mark.parametrize('busy,hold,health,status,stale,expected', [
    (False,False,False,'exited',False,True), (True,False,False,'exited',False,False),
    (False,True,False,'exited',False,False), (False,False,True,'exited',False,False),
    (False,False,False,'running',False,False), (False,False,False,'exited',True,False)])
def test_container_recovery_never_interrupts_active_or_held_business(busy,hold,health,status,stale,expected):
    p = packet(datetime(2026,10,13,10,tzinfo=g.TZ))
    p['safety']['busy'] = busy; p['maintenance_hold'] = hold
    p['current']['health_ok'] = p['previous']['health_ok'] = health
    p['current']['container']['Status'] = status
    found = ['sample_missing_or_stale'] if stale else []
    assert ('start_stopped_container' in g.candidates(p,found)) is expected


def test_do_not_restore_disabled_or_procurement_timer():
    now = datetime(2026,10,13,10,tzinfo=g.TZ); p = packet(now)
    p['units'] = {'ul-auto-sync.timer': {'UnitFileState':'disabled','ActiveState':'inactive'},
                  'ul-web-purchase-enqueue.timer': {'UnitFileState':'enabled','ActiveState':'inactive'}}
    assert g.candidates(p,g.issues(p,now)) == []
    p['units']['ul-auto-sync.timer']['UnitFileState'] = 'enabled'
    assert g.candidates(p,g.issues(p,now)) == ['start_timer:ul-auto-sync.timer']


def test_cooldown_and_daily_caps_include_failed_attempts():
    now = datetime(2026,10,13,10,tzinfo=g.TZ)
    action = 'start_stopped_container'
    attempts = [{'at':now.replace(hour=1).isoformat(),'action':action,'result':'failed'}]
    assert not g.permitted(action,attempts,now)
    action = 'retry_failed_backup'
    assert g.permitted(action,attempts,now)
    attempts.append({'at':now.isoformat(),'action':action,'result':'dispatching'})
    assert not g.permitted(action,attempts,now)


def test_unknown_actions_cannot_run_shell():
    with pytest.raises(ValueError, match='action_not_allowed'):
        g.execute('rm -rf /', Path('/nope'))


def test_model_cannot_authorize_repairs():
    now = datetime(2026,10,13,10,tzinfo=g.TZ); p = packet(now)
    p['codex'] = {'suggestion':'restart everything and pay all orders'}
    assert g.candidates(p,[]) == []


def test_sqlite_inspection_preserves_rows_and_fails_closed(tmp_path):
    path = tmp_path/'workbench.sqlite3'
    with sqlite3.connect(path) as db:
        for name in ('print_jobs','update_jobs','web_operations'):
            db.execute(f'CREATE TABLE {name}(status TEXT)')
        db.execute("INSERT INTO print_jobs VALUES ('unknown')")
    before = path.read_bytes()
    assert g.safety(tmp_path)['busy']
    assert not g.safety(tmp_path)['heavy_busy']
    assert before == path.read_bytes()
    path.unlink()
    assert g.safety(tmp_path) == {'ok':False,'busy':True}


def test_duplicate_and_truncated_samples_not_counted(tmp_path):
    now = datetime(2026,10,13,10,tzinfo=g.TZ)
    d = tmp_path/'observation-20261012'; d.mkdir()
    row = packet(now)['current']
    (d/'2026-10-13.jsonl').write_text(json.dumps(row)+'\n'+json.dumps(row)+'\n{"broken')
    assert len(g.samples(tmp_path,now)) == 1


def test_final_run_does_not_call_codex_or_execute(tmp_path,monkeypatch):
    monkeypatch.setattr(g,'stamp',lambda:g.END)
    monkeypatch.setattr(g,'execute',lambda *a:pytest.fail('end must not execute repairs'))
    monkeypatch.setattr(g,'diagnose',lambda *a:pytest.fail('end must not use Codex'))
    result = g.run(data=tmp_path,state_root=tmp_path/'state',force_codex=True)
    assert result['status']=='observation_complete'
    assert (tmp_path/'state/final.json').exists()


def test_timer_revalidated_before_start(monkeypatch,tmp_path):
    monkeypatch.setattr(g,'units',lambda:{'ul-auto-sync.timer':{'UnitFileState':'disabled'}})
    monkeypatch.setattr(g,'command',lambda *a:pytest.fail('must not start disabled timer'))
    assert g.execute('start_timer:ul-auto-sync.timer',tmp_path)=='state_changed_or_hold'


def test_recovery_intent_is_saved_before_dispatch(tmp_path,monkeypatch):
    now = datetime(2026,10,13,10,tzinfo=g.TZ); p = packet(now)
    p['units'] = {'ul-auto-sync.timer':{'UnitFileState':'enabled','ActiveState':'inactive'}}
    monkeypatch.setattr(g,'stamp',lambda:now)
    monkeypatch.setattr(g,'collect',lambda *a:p)
    monkeypatch.setattr(g,'samples',lambda *a:[])
    state = tmp_path/'state'; state.mkdir()
    g.write(state/'state.json', {'attempts':[],'last_codex_at':now.isoformat()})
    def execute(action,data):
        assert g.read(state/'state.json',{})['attempts'][-1]['result']=='dispatching'
        return 'started'
    monkeypatch.setattr(g,'execute',execute)
    result = g.run(tmp_path,state)
    assert result['repairs'][0]['result']=='started'
