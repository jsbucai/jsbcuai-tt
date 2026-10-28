"""Shared summaries, actual lines and a bounded, non-replayable correction."""
import copy
import json
import uuid
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_order_print_state import app, order, put_lifecycle
from tests.test_print_previews import preview, prepare, report, fact
from tests.test_business_rules import facts, TODAY
from core.rules import parse_note, parse_summary, note_delivery_date
from core.fields import order_fields, order_total
from core.print_jobs import JobConflict, print_readiness
from core.print_corrections import preview as correction_preview, apply as correct
from core.store import dumps, Store
from core.business_rules import calculate_snapshot

ADMIN = {'id': 'admin', 'display_name': '管理员', 'is_admin': True}
SHARED = '客户单#项目#2#图号#双头加螺母#M36*180#10.9#发黑#24#FB1219'


@pytest.mark.parametrize('suffix,expected', [
    ('#交期=2026-9-6', '2026-09-06'), ('＃交期：2026/09/06', '2026-09-06'),
    ('#交期=2026年9月6日', '2026-09-06'), ('#交期=2025.09.20', '2025-09-20'),
    ('', None), ('#项目=2026-09-06', None)])
def test_explicit_dates_only(suffix, expected):
    assert note_delivery_date(SHARED + suffix) == expected


@pytest.mark.parametrize('suffix', ['#交期=9/6', '#交期=2026-02-30', '#交期=2026-09-06#交期=2026-09-07'])
def test_ambiguous_dates_stay_unknown_and_printable(suffix):
    row = {'order_no': 'ERP1', 'sku': 'LM2562', 'product_name': '螺母', 'quantity': 48, 'system_note': SHARED + suffix}
    assert parse_summary(row['system_note'])['delivery_date_status'] == 'invalid'
    assert print_readiness('order', row)[0]
    assert order_fields(row)['qrValue'] == 'ERP1'


def test_person_not_sku_and_shared_descriptions_use_actual_quantity():
    note = SHARED.replace('FB1219', '刘兆杰')
    parsed = parse_note(note)
    assert parsed['summaryPerson'] == '刘兆杰' and parsed['sku'] == ''
    row = dict(order('a', 'ERP1', 'LM2562', 'nut', '', '', ''), system_note=note, quantity=48)
    label = order_fields(row)
    assert label['sku'] == 'LM2562' and label['quantity'] == '48'
    assert label['name'] == '双头加螺母' and label['spec'] == 'M36*180'
    assert label['summaryPerson'] == '刘兆杰' and label['orderNo'] == '客户单'
    row['quantity'] = ''
    assert order_total(row) == 0 and not print_readiness('order', row)[0]


def test_extra_separator_unique_repair_preserves_legitimate_empty_position():
    raw = 'PO#P#1#D#销#20*100#35#   #发黑#2#XL0467#交期=2026-08-26'
    result = parse_summary(raw)
    assert result['raw'] == raw and result['normalized']
    assert result['fields']['grade'] == '35' and result['fields']['surface'] == '发黑'
    assert result['fields']['sku'] == 'XL0467'
    legitimate = parse_note('PO#P#1##销#20*100#35##2#XL0467')
    assert legitimate['drawingNo'] == '' and legitimate['surface'] == ''
    assert legitimate['name'] == '销'


@pytest.mark.parametrize('summary', ['', '销售【螺栓】等【1】种商品给客户'])
def test_missing_structured_summary_uses_catalog(summary):
    row = dict(order('a', 'ERP1', 'LS1', 'x', '', '', ''), system_note=summary, spec='M10*30')
    fields = order_fields(row)
    assert fields['name'] == '螺栓' and fields['spec'] == 'M10*30'
    assert print_readiness('order', row) == (True, '')
    assert parse_summary(summary)['delivery_date_status'] == 'missing'


def test_two_lines_same_summary_and_repeated_sku_keep_two_labels(app):
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        for index, record in enumerate(db.execute("SELECT id,payload FROM records WHERE kind='orders' AND id LIKE 'line-a%'")):
            row = json.loads(record['payload'])
            row.update(system_note=SHARED, sku='LM2562', quantity=24*(index+1))
            db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id=?", (dumps(row), record['id']))
    value = preview(app, ['ORDER-A'])
    assert value['order_count'] == 1 and value['line_count'] == value['label_count'] == 2
    assert [line['quantity'] for line in value['orders'][0]['lines']] == [24, 48]
    job = prepare(app, value)
    assert [page['fields']['quantity'] for page in job['pages']] == ['24', '48']
    assert {page['fields']['name'] for page in job['pages']} == {'双头加螺母'}
    assert len({page['line_ref']['line_id'] for page in job['pages']}) == 2


def test_full_stock_mixed_prefix_has_local_pack_plan():
    data = facts()
    second = copy.deepcopy(data['orders'][0]['lines'][0])
    second.update(line_id='nut', product_id='nuts', product_code='LM1')
    data['orders'][0]['lines'].append(second)
    stock = copy.deepcopy(data['inventory'][0]); stock['product_id'] = 'nuts'
    data['inventory'].append(stock)
    result = calculate_snapshot(data, today=TODAY)
    assert not result['conflicts']
    plan = result['plans'][0]
    assert plan['freightname'] == '库存件组合' and plan['write_enabled'] is False
    data['inventory'][-1].update(physical_qty='0', saleable_qty='-8')
    assert calculate_snapshot(data, today=TODAY)['plans'][0]['freightname'] == '需采购'


def test_missing_date_does_not_block_fifo_but_disables_writes():
    data = facts(); data['orders'][0]['delivery_date'] = None
    data['orders'][0]['lines'][0]['delivery_date'] = None
    result = calculate_snapshot(data, today=TODAY)
    assert not result['conflicts'] and result['allocations'][0]['actual'] == 8
    assert result['plans'][0]['write_enabled'] is False and result['plans'][0]['freightnumber'] == ''


def paused(store):
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('printing_maintenance','true')")


def correct_request(value):
    return {'correction_id': value['correction_id'], 'preview_hash': value['preview_hash'],
            'idempotency_key': uuid.uuid4().hex, 'devices_stopped': True}


def test_correction_revokes_old_claim_preserves_history_and_ignores_late_result(app):
    jobs = app.extensions['service']['jobs']; store = jobs.store
    old = prepare(app, preview(app, ['ORDER-A']))
    claim_args = {'user_initiated': True, 'idempotency_key': uuid.uuid4().hex, 'device_id': 'phone-a'}
    claim = jobs.claim(old['job_id'], claim_args)
    done = prepare(app, preview(app, ['ORDER-B'])); report(app, done)
    pending = prepare(app, preview(app, ['ORDER-C'])); report(app, pending, status='unknown')
    with store.connect(write=True) as db:
        db.execute("INSERT INTO order_print_states VALUES ('profile-a','MISSING','printed','','','old','','old')")
        history = db.execute('SELECT count(*) FROM order_print_audit').fetchone()[0]
    value = correction_preview(store, ADMIN)
    assert value['order_count'] == 3 and value['line_count'] == 4 and len(value['jobs']) == 3
    request = correct_request(value)
    with pytest.raises(JobConflict, match='暂停打印'): correct(store, ADMIN, request)
    paused(store)
    result = correct(store, ADMIN, request)
    assert result['revoked_job_count'] == 3
    for number in ('ORDER-A', 'ORDER-B', 'ORDER-C'):
        assert fact(app, number)['state'] == 'unprinted'
    for job in (old, done, pending):
        assert jobs.get(job['job_id'])['status'] == 'revoked'
    for action in (
        lambda: jobs.claim(old['job_id'], claim_args),
        lambda: jobs.result(old['job_id'], {'claim_token': claim['claim_token'], 'status': 'submitted', 'bytes_sent': 100}),
        lambda: jobs.recover(old['job_id'], {'idempotency_key': claim_args['idempotency_key'], 'device_id': 'phone-a', 'reason': '恢复'}),
    ):
        with pytest.raises(JobConflict) as error: action()
        assert error.value.code == 'print_job_revoked'
    Store(store.runtime)
    assert correct(store, ADMIN, request)['replayed']
    with store.connect() as db:
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='MISSING'").fetchone()[0] == 'printed'
        assert db.execute('SELECT count(*) FROM order_print_audit').fetchone()[0] == history + 3
        assert db.execute("SELECT count(*) FROM order_print_audit WHERE to_state='printed'").fetchone()[0] == 1
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value='false' WHERE key='printing_maintenance'")
    new = prepare(app, preview(app, ['ORDER-B'])); report(app, new)
    assert correct(store, ADMIN, request)['replayed'] and fact(app, 'ORDER-B')['state'] == 'printed'


def test_correction_concurrent_calls_only_apply_once_and_scope_version_checked(app):
    store = app.extensions['service']['store']
    report(app, prepare(app, preview(app, ['ORDER-B'])))
    value = correction_preview(store, ADMIN); request = correct_request(value); paused(store)
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value='\"changed\"' WHERE key='data_version'")
    with pytest.raises(JobConflict, match='已变化'): correct(store, ADMIN, request)
    assert fact(app, 'ORDER-B')['state'] == 'printed'
    value = correction_preview(store, ADMIN); request = correct_request(value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: correct(store, ADMIN, request), range(2)))
    assert sum(not r['replayed'] for r in results) == 1


def test_correction_excludes_erp_shipped_and_rejects_nonadmin(app):
    store = app.extensions['service']['store']
    report(app, prepare(app, preview(app, ['ORDER-B'])))
    put_lifecycle(store, 'shipped_unposted', '500')
    with pytest.raises(JobConflict, match='管理员'): correction_preview(store, {'id': 'packer'})
    value = correction_preview(store, ADMIN)
    assert value['order_count'] == 0 and len(value['excluded']) == 1
    paused(store); correct(store, ADMIN, correct_request(value))
    assert fact(app, 'ORDER-B')['state'] == 'printed'


def test_line_counts_and_exact_order_qr_include_blocked_lines(app):
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        record = db.execute("SELECT payload FROM records WHERE id='line-c1'").fetchone()
        row = json.loads(record[0]); row.update(print_blocked=True, print_block_reason='身份待核对')
        db.execute("UPDATE records SET payload=? WHERE id='line-c1'", (dumps(row),))
    client = app.test_client()
    result = client.get('/api/v2/orders?print_state=unprinted&count_basis=lines').get_json()
    assert result['line_count'] == result['total'] == 4 and result['order_count'] == 3
    assert result['line_state_counts']['unprinted'] == 4
    for q in ('ORDER-A', '@10/01#ORDER-A'):
        result = client.get('/api/v2/orders', query_string={'q': q}).get_json()
        assert len(result['items']) == 2 and {r['order_no'] for r in result['items']} == {'ORDER-A'}


def test_shared_old_job_is_revoked_without_resetting_shipped_or_absent_orders(app):
    store = app.extensions['service']['store']; jobs = app.extensions['service']['jobs']
    old = prepare(app, preview(app, ['ORDER-A', 'ORDER-B', 'ORDER-C']))
    jobs.claim(old['job_id'], {'user_initiated': True, 'idempotency_key': uuid.uuid4().hex, 'device_id': 'phone-a'})
    put_lifecycle(store, 'shipped_unposted', '500')
    with store.connect(write=True) as db:
        db.execute("DELETE FROM records WHERE kind='orders' AND id='line-c1'")
        original = [tuple(r) for r in db.execute("SELECT * FROM order_print_states WHERE order_no IN ('ORDER-B','ORDER-C') ORDER BY order_no")]
        old_audit = db.execute('SELECT count(*) FROM order_print_audit').fetchone()[0]
    value = correction_preview(store, ADMIN)
    assert value['order_count'] == 1 and value['line_count'] == 2
    assert {r['order_no'] for r in value['jobs'][0]['preserved_order_refs']} == {'ORDER-B', 'ORDER-C'}
    paused(store); correct(store, ADMIN, correct_request(value))
    assert jobs.get(old['job_id'])['status'] == 'revoked'
    assert fact(app, 'ORDER-A')['state'] == 'unprinted'
    with store.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM order_print_states WHERE order_no IN ('ORDER-B','ORDER-C') ORDER BY order_no")] == original
        assert db.execute('SELECT count(*) FROM order_print_audit').fetchone()[0] == old_audit + 1


def test_content_changed_review_is_corrected_and_old_job_revoked(app):
    store = app.extensions['service']['store']; jobs = app.extensions['service']['jobs']
    old = prepare(app, preview(app, ['ORDER-B'])); report(app, old)
    with store.connect(write=True) as db:
        db.execute("UPDATE order_print_states SET state='unprinted',reason='content_changed',job_id='' WHERE order_no='ORDER-B'")
    value = correction_preview(store, ADMIN)
    assert value['order_count'] == 1 and len(value['jobs']) == 1
    paused(store); result = correct(store, ADMIN, correct_request(value))
    assert result['already_unprinted_review_count'] == 1
    assert fact(app, 'ORDER-B')['reason'].startswith('correction:')
    assert jobs.get(old['job_id'])['status'] == 'revoked'


def test_specified_order_correction_preserves_other_active_order_facts(app):
    store=app.extensions['service']['store'];jobs=app.extensions['service']['jobs']
    old=prepare(app,preview(app,['ORDER-A','ORDER-B']))
    jobs.claim(old['job_id'],{'user_initiated':True,'idempotency_key':uuid.uuid4().hex,'device_id':'phone-a'})
    with store.connect() as db:
        before=tuple(db.execute("SELECT * FROM order_print_states WHERE order_no='ORDER-B'").fetchone())
    selected=[{'profile_id':old['orders'][0]['profile_id'],'order_no':'ORDER-A'}]
    value=correction_preview(store,ADMIN,'specified_orders',selected)
    assert value['order_count']==1
    paused(store);correct(store,ADMIN,correct_request(value))
    assert fact(app,'ORDER-A')['state']=='unprinted'
    with store.connect() as db:
        assert before==tuple(db.execute("SELECT * FROM order_print_states WHERE order_no='ORDER-B'").fetchone())
    assert jobs.get(old['job_id'])['status']=='revoked'
