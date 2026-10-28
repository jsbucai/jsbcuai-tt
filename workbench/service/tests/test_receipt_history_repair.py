import copy
import json
import sqlite3

import pytest

from core.business_rules import calculate_snapshot
from core.receipt_history_repair import repair, verify_proof
from test_business_rules import facts, event, TODAY, KEY


def scenario():
    def source(qty, events=()):
        result = facts(str(qty), events=list(events))
        result['orders'][0]['order_no'] = '26976236#98'
        result['orders'][0]['lines'][0].update(open_qty='240', product_code='DQ0861')
        result['inventory'][0]['saleable_qty'] = str(qty - 240)
        return result
    receipt = dict(event('250'), posted_at='2026-09-25T14:16:07+08:00')
    before = calculate_snapshot(source(28), today=TODAY)
    transition = calculate_snapshot(source(278), before['receipt_state'], TODAY)
    late = calculate_snapshot(source(278, [receipt]), transition['receipt_state'], TODAY)
    assert not before['conflicts'] and not transition['conflicts'] and not late['conflicts']
    key = next(iter(late['receipt_state']['orders']))
    saved = {}
    for name, calc, time in [('b',before,'14:02:29'),('t',transition,'14:17:42'),('l',late,'14:32:45')]:
        stamp = {'job_id':name, 'calculated_at':'2026-09-25T'+time}
        saved[name] = {'first':stamp, 'last':stamp, 'snapshot':{
            'state':calc['receipt_state']['orders'][key], 'allocation':calc['allocations'][0],
            'events':{'receipt-1':calc['receipt_state']['events'].get('receipt-1')}}}
    entry = late['receipt_state']['orders'][key]
    target = {'ledger_key':key, 'product_code':'DQ0861',
              'event':late['receipt_state']['events']['receipt-1'],
              'part':entry['receipts'][0], 'expected':entry,
              'before_job':'b','transition_job':'t','later_job':'l'}
    manifest = {'id':'repair-test-1','targets':[target]}
    db = sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE update_jobs(status TEXT);
        CREATE TABLE records(kind TEXT,payload TEXT);
        CREATE TABLE print_jobs(id TEXT,status TEXT,frozen TEXT);
        CREATE TABLE order_print_states(order_no TEXT,state TEXT);
        CREATE TABLE order_print_audit(order_no TEXT,note TEXT);''')
    db.execute('INSERT INTO settings VALUES (?,?)', ('receipt_ledger_v1',json.dumps(late['receipt_state'])))
    db.execute('INSERT INTO records VALUES (?,?)', ('orders',json.dumps(dict(KEY,
        order_no='26976236#98', lifecycle_state='active',quantity='240'))))
    db.execute('INSERT INTO print_jobs VALUES (?,?,?)',('old-print','claimed','frozen-original'))
    db.execute('INSERT INTO order_print_states VALUES (?,?)',('26976236#98','printed'))
    db.execute('INSERT INTO order_print_audit VALUES (?,?)',('26976236#98','original operator/time'))
    db.commit()
    return db, manifest, copy.deepcopy(saved), source(278,[receipt])


def loader(saved):
    return lambda db, job, key, eid: copy.deepcopy(saved[job])


def print_facts(db):
    return [db.execute('SELECT * FROM '+t).fetchall() for t in
            ('print_jobs','order_print_states','order_print_audit')]


def test_dq0861_repair_survives_refresh_serialization_and_repeated_runs():
    db, manifest, saved, source = scenario()
    prints = print_facts(db)
    original = db.execute('SELECT value FROM settings').fetchone()[0]
    assert repair(db,manifest,loader(saved),dry_run=True)['changed_count']==1
    assert db.execute('SELECT value FROM settings').fetchone()[0]==original
    result = repair(db,manifest,loader(saved))
    assert result['changed_count']==1
    assert repair(db,manifest,loader(saved))['already_applied']
    state = json.loads(db.execute('SELECT value FROM settings').fetchone()[0])
    assert state['balances']==json.loads(original)['balances']
    assert state['events']==json.loads(original)['events']
    for _ in range(3):
        calculated=calculate_snapshot(source,json.loads(json.dumps(state)),TODAY)
        assert not calculated['conflicts']
        row=calculated['allocations'][0]
        assert row['category']=='来货件' and row['actual']==240
        assert row['inventory_quantity']==28 and row['receipt_allocations'][0]['quantity']==212
        state=calculated['receipt_state']
    assert print_facts(db)==prints


@pytest.mark.parametrize('change', ['late_receipt','wrong_quantity','wrong_identity','no_shortage','event_already_known'])
def test_unproven_histories_cannot_be_repaired(change):
    db,m,s,_=scenario();t=m['targets'][0]
    if change=='late_receipt':t['event']['posted_at']='2026-09-25T14:20:00+08:00'
    if change=='wrong_quantity':s['t']['snapshot']['allocation']['stock']+=1
    if change=='wrong_identity':s['b']['snapshot']['allocation']['demand']['sku']='wrong'
    if change=='no_shortage':s['b']['snapshot']['state']['actual']=240
    if change=='event_already_known':s['t']['snapshot']['events']['receipt-1']=t['event']
    with pytest.raises(ValueError):verify_proof(t['ledger_key'],t['event'],t['part'],s['b'],s['t'],s['l'])


@pytest.mark.parametrize('change', ['version','shipped','identity','running','duplicate','bad_second_proof'])
def test_batch_rejects_conflicts_atomically_and_keeps_prints(change):
    db,m,s,_=scenario();before=db.execute('SELECT value FROM settings').fetchone()[0];prints=print_facts(db)
    if change=='version':m['targets'][0]['expected']=dict(m['targets'][0]['expected'],actual=1)
    if change in ('shipped','identity'):
        r=json.loads(db.execute('SELECT payload FROM records').fetchone()[0])
        r['shadow_demand' if change=='shipped' else 'sku_id']=True if change=='shipped' else 'other'
        db.execute('UPDATE records SET payload=?',(json.dumps(r),));db.commit()
    if change=='running':db.execute("INSERT INTO update_jobs VALUES ('running')");db.commit()
    if change=='duplicate':m['targets'].append(copy.deepcopy(m['targets'][0]))
    if change=='bad_second_proof':
        # Validation fails after the valid target, but before any ledger write.
        t=copy.deepcopy(m['targets'][0]);t['event']['id']='missing';m['targets'].append(t)
    with pytest.raises(ValueError):repair(db,m,loader(s))
    assert db.execute('SELECT value FROM settings').fetchone()[0]==before
    assert print_facts(db)==prints


def test_idempotency_key_cannot_be_reused_with_a_new_scope():
    db,m,s,_=scenario();repair(db,m,loader(s))
    m['targets'][0]['product_code']='new'
    with pytest.raises(ValueError,match='变更范围'):repair(db,m,loader(s))


def test_old_adapter_missing_posted_time_and_supplier_fields_is_supported():
    db,m,s,_=scenario()
    s['l']['snapshot']['events']['receipt-1'].pop('posted_at')
    assert repair(db,m,loader(s))['changed_count']==1
