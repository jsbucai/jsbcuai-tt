"""Concurrent preview segments, reprint confirmation and fact recovery."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from tests.test_order_print_state import app, order
from core.print_jobs import JobConflict
from core.order_print_state import print_state_details
from core.store import dumps
ACTOR = {'id': 'packer-a', 'display_name': '包装甲'}

def selection(app, numbers, purpose='print'):
    store = app.extensions['service']['store']; identity = uuid.uuid4().hex
    with store.connect(write=True) as db:
        db.execute('CREATE TABLE IF NOT EXISTS daily_work_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute("INSERT OR REPLACE INTO daily_work_settings VALUES ('task_version','1')")
        db.execute('''CREATE TABLE IF NOT EXISTS daily_work_tasks(id TEXT PRIMARY KEY,
            profile_id TEXT,order_no TEXT,role TEXT,version INTEGER,condition_state TEXT)''')
        objects = []
        for number in numbers:
            tid = 'print-task-' + number
            db.execute('''INSERT OR REPLACE INTO daily_work_tasks
                (id,profile_id,order_no,role,version,condition_state)
                VALUES (?,? ,?,'stock_packing',1,'active')''', (tid, 'profile-a', number))
            objects.append({'kind': 'task', 'id': tid, 'version': 1})
        db.execute('INSERT INTO daily_work_selections VALUES (?,?,?,?,?,?,?)',
            (identity, ACTOR['id'], 'stock_packing', 'orders-v1', 1,
             dumps({'purpose': purpose, 'objects': objects, 'order_nos': numbers, 'profile_id': 'profile-a'}), '2026-10-08'))
    return identity

def preview(app, numbers, **kwargs):
    return app.extensions['service']['jobs'].preview({'selection_id': selection(app, numbers),
        'device_id': 'phone-a', 'expected_data_version': 'orders-v1', **kwargs}, actor=ACTOR)

def prepare(app, value, segment=1, **kwargs):
    return app.extensions['service']['jobs'].prepare({'kind': 'order', 'preview_id': value['preview_id'],
        'preview_hash': value['preview_hash'], 'segment_index': segment, 'device_id': 'phone-a',
        'prepare_idempotency_key': uuid.uuid4().hex, **kwargs}, actor=ACTOR)

def report(app, job, status='submitted', count=10):
    jobs = app.extensions['service']['jobs']
    claim = jobs.claim(job['job_id'], {'user_initiated': True, 'idempotency_key': uuid.uuid4().hex, 'device_id': 'phone-a'})
    return jobs.result(job['job_id'], {'claim_token': claim['claim_token'], 'device_id': 'phone-a', 'status': status, 'bytes_sent': count})

def fact(app, number):
    with app.extensions['service']['store'].connect() as db:
        return print_state_details(db, {('profile-a', number)})[('profile-a', number)]

def test_preview_does_not_reserve_and_counts_whole_orders(app):
    value = preview(app, ['ORDER-A', 'ORDER-B'])
    assert value['order_count'] == 2 and value['label_count'] == 3
    assert value['segments'] == [{'index': 1, 'order_nos': ['ORDER-A', 'ORDER-B'], 'order_count': 2, 'label_count': 3}]
    assert fact(app, 'ORDER-A')['state'] == 'unprinted'
    with app.extensions['service']['store'].connect() as db:
        assert not db.execute('SELECT 1 FROM print_jobs').fetchone()

def test_legacy_selection_and_changed_task_rejected(app):
    jobs = app.extensions['service']['jobs']
    sid = selection(app, ['ORDER-B'], 'action')
    body = {'selection_id': sid, 'device_id': 'phone-a', 'expected_data_version': 'orders-v1'}
    with pytest.raises(JobConflict, match='不支持打印预览'): jobs.preview(body, actor=ACTOR)
    sid = selection(app, ['ORDER-B'])
    with jobs.store.connect(write=True) as db: db.execute("UPDATE daily_work_tasks SET version=2 WHERE order_no='ORDER-B'")
    with pytest.raises(JobConflict, match='所选任务已变化'): jobs.preview({**body, 'selection_id': sid}, actor=ACTOR)

def test_concurrent_segment_different_prepare_keys_reserves_only_once(app):
    value = preview(app, ['ORDER-B'])
    def attempt(_):
        try: return prepare(app, value)['job_id']
        except JobConflict as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool: answers = list(pool.map(attempt, range(2)))
    assert sum(len(a) == 32 for a in answers) == 1
    assert 'print_preview_used' in answers

@pytest.mark.parametrize('status', ['cancelled', 'failed'])
@pytest.mark.parametrize('prior_status', ['submitted', 'unknown'])
def test_reprint_zero_bytes_restores_prior_state_and_actor(app, status, prior_status):
    first = prepare(app, preview(app, ['ORDER-B'])); report(app, first, prior_status)
    prior = fact(app, 'ORDER-B'); value = preview(app, ['ORDER-B'])
    assert value['reprint_count'] + value['uncertain_count'] == 1
    with pytest.raises(JobConflict, match='明确确认'): prepare(app, value)
    second = prepare(app, value, confirm_reprint=True, confirm_uncertain=True)
    report(app, second, status, 0)
    restored = fact(app, 'ORDER-B')
    assert restored['state'] == prior['state'] and restored['job_id'] == prior['job_id']
    assert restored['latest_print_actor_name'] == '包装甲'
    with app.extensions['service']['store'].connect() as db:
        assert db.execute("SELECT count(*) FROM order_print_audit WHERE order_no='ORDER-B'").fetchone()[0] == 4

def test_prepared_reprint_cancel_restores_prior_state(app):
    first = prepare(app, preview(app, ['ORDER-B'])); report(app, first)
    second = prepare(app, preview(app, ['ORDER-B']), confirm_reprint=True)
    app.extensions['service']['jobs'].cancel(second['job_id'], {'device_id': 'phone-a', 'idempotency_key': uuid.uuid4().hex, 'reason': '不打印'})
    assert fact(app, 'ORDER-B')['state'] == 'printed'

def test_missing_byte_count_reprint_failure_is_uncertain_and_lock_names_employee(app):
    first = prepare(app, preview(app, ['ORDER-B'])); report(app, first)
    second = prepare(app, preview(app, ['ORDER-B']), confirm_reprint=True)
    with pytest.raises(JobConflict, match='包装甲'): preview(app, ['ORDER-B'])
    report(app, second, 'failed', None)
    assert fact(app, 'ORDER-B')['state'] == 'uncertain'

def test_segments_survive_own_success_no_whole_selection_cap(app):
    jobs = app.extensions['service']['jobs']; numbers = []
    with jobs.store.connect(write=True) as db:
        for index in range(505):
            number = f'BIG-{index:03d}'; numbers.append(number)
            item = order(number, number, 'SKU-Z', 'sku-z', '', '', '完整')
            db.execute('INSERT INTO records(kind,id,payload,sku,buyer,note,keywords,quantity,amount,logistics) VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('orders', item['id'], dumps(item), 'SKU-Z', '', '', '', 1, 0, 'LS库存件'))
    value = preview(app, numbers)
    assert [s['order_count'] for s in value['segments']] == [100, 100, 100, 100, 100, 5]
    first = prepare(app, value, 1); report(app, first)
    with jobs.store.connect(write=True) as db: db.execute("UPDATE daily_work_settings SET value='2' WHERE key='task_version'")
    assert prepare(app, value, 2)['order_count'] == 100
    with pytest.raises(JobConflict, match='已使用'): prepare(app, value, 1, confirm_reprint=True)

def test_other_device_or_changed_label_invalidates_remaining_preview(app):
    jobs = app.extensions['service']['jobs']; value = preview(app, ['ORDER-B'])
    jobs.prepare({'kind': 'order', 'order_nos': ['ORDER-B'], 'device_id': 'phone-b', 'expected_data_version': 'orders-v1'},
        actor={'id': 'b', 'display_name': '包装乙'})
    with pytest.raises(JobConflict, match='包装乙'): prepare(app, value)
    assert fact(app, 'ORDER-B')['current_print_actor_name'] == '包装乙'

def test_label_changed_without_data_version_change_is_rejected(app):
    value = preview(app, ['ORDER-B']); store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        item = json.loads(db.execute("SELECT payload FROM records WHERE id='line-b1'").fetchone()[0]); item['quantity'] = '2'; item['system_note'] = item['system_note'].replace('#1#SKU-C', '#2#SKU-C')
        db.execute("UPDATE records SET payload=? WHERE id='line-b1'", (dumps(item),))
    with pytest.raises(JobConflict, match='标签内容已变化'): prepare(app, value)
    assert fact(app, 'ORDER-B')['state'] == 'unprinted'

def test_reset_version_idempotency_and_reserved_lock(app):
    jobs = app.extensions['service']['jobs']; first = prepare(app, preview(app, ['ORDER-B'])); report(app, first)
    value = preview(app, ['ORDER-B'])['orders'][0]
    payload = {'profile_id': 'profile-a', 'device_id': 'phone-a', 'reason': '用户确认恢复',
        'idempotency_key': uuid.uuid4().hex, 'expected_state_revision': value['state_revision'],
        'expected_label_fingerprint': value['label_fingerprint']}
    assert not jobs.reset_order_state('ORDER-B', payload, actor=ACTOR)['replayed']
    assert jobs.reset_order_state('ORDER-B', payload, actor=ACTOR)['replayed']
    with pytest.raises(JobConflict, match='状态或标签已变化'):
        jobs.reset_order_state('ORDER-B', {**payload, 'idempotency_key': uuid.uuid4().hex}, actor=ACTOR)
    prepare(app, preview(app, ['ORDER-B']))
    with pytest.raises(JobConflict, match='包装甲'): jobs.reset_order_state('ORDER-B', {'profile_id': 'profile-a'}, actor=ACTOR)


def test_server_actor_cannot_be_changed_by_client_and_claim_is_actor_bound(app):
    value = preview(app, ['ORDER-B'])
    with pytest.raises(ValueError, match='不接受更改'):
        prepare(app, value, actor_id='admin')
    job = prepare(app, value)
    assert job['actor_id'] == ACTOR['id'] and job['actor_name'] == ACTOR['display_name']
    with pytest.raises(JobConflict, match='其他人员'):
        app.extensions['service']['jobs'].claim(job['job_id'], {
            'user_initiated': True, 'device_id': 'phone-a', 'idempotency_key': uuid.uuid4().hex},
            actor={'id': 'other', 'display_name': '其他'})
    assert fact(app, 'ORDER-B')['state'] == 'reserved'


def test_single_order_is_never_split_between_segments(app):
    value = preview(app, ['ORDER-A', 'ORDER-B'], max_per_package='0.02')
    assert [s['order_nos'] for s in value['segments']] == [['ORDER-A'], ['ORDER-B']]
    assert [s['label_count'] for s in value['segments']] == [100, 50]
    with pytest.raises(ValueError, match='整单 ORDER-A'):
        preview(app, ['ORDER-A'], max_per_package='0.01')


def test_preview_actor_or_device_cannot_adopt_other_previews(app):
    jobs = app.extensions['service']['jobs']; value = preview(app, ['ORDER-B'])
    with pytest.raises(JobConflict, match='其他人员或设备'):
        jobs.prepare({'kind': 'order', 'preview_id': value['preview_id'], 'preview_hash': value['preview_hash'],
            'device_id': 'phone-b', 'segment_index': 1, 'prepare_idempotency_key': uuid.uuid4().hex}, actor=ACTOR)


def test_old_claim_survives_v10_and_records_result_without_reissue(app):
    jobs = app.extensions['service']['jobs']
    job = jobs.prepare({'kind': 'order', 'order_nos': ['ORDER-B'], 'device_id': 'phone-a',
                        'expected_data_version': 'orders-v1'})
    claim = jobs.claim(job['job_id'], {'user_initiated': True, 'idempotency_key': uuid.uuid4().hex,
        'device_id': 'phone-a'}, actor=ACTOR)
    assert claim['actor_name'] == '包装甲'
    result = jobs.result(job['job_id'], {'claim_token': claim['claim_token'], 'device_id': 'phone-a',
        'status': 'submitted', 'bytes_sent': None, 'message': '人工确认出纸完成'})
    assert result['status'] == 'submitted' and fact(app, 'ORDER-B')['state'] == 'printed'
    with jobs.store.connect() as db:
        key = db.execute('SELECT idempotency_key FROM print_jobs WHERE id=?', (job['job_id'],)).fetchone()[0]
    replay = jobs.claim(job['job_id'], {'user_initiated': True, 'idempotency_key': key,
        'device_id': 'phone-a'}, actor=ACTOR)
    assert replay['replayed'] and 'claim_token' not in replay and 'cpcl_base64' not in replay


from tests.test_daily_work import store, seed, enable_assignment, user, NOW, order as daily_order
from core.daily_work import DailyWork, DailyWorkConflict, PRINT_REVIEW_BLOCK, LABEL_REVIEW_BLOCK

@pytest.mark.parametrize('legacy_status', ['packed', 'resolved'])
def test_legacy_packed_resolved_owner_can_reprint_and_reset_but_other_staff_cannot(store, legacy_status):
    seed(store, [daily_order('S1', 'LS0001', 'LS库存件')], {'S1': 'LS库存件'})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine); engine.refresh()
    actor = user('packer', ['stock_packing'])
    with store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET status=? WHERE role='stock_packing' AND order_no='S1'", (legacy_status,))
    assert engine.assert_reprint_access(actor, ['S1'], 'P')
    assert engine.assert_print_reset_access(actor, ['S1'], 'P')
    with pytest.raises(DailyWorkConflict): engine.assert_reprint_access(user('sales', ['sales']), ['S1'], 'P')
    with pytest.raises(DailyWorkConflict): engine.assert_print_reset_access(user('sales', ['sales']), ['S1'], 'P')


def test_reprint_confirmation_only_relaxes_print_review_block_not_source_identity_or_label(store):
    seed(store, [daily_order('S1', 'LS0001', 'LS库存件')], {'S1': 'LS库存件'})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine); engine.refresh(); actor = user('packer', ['stock_packing'])
    def block(value):
        with store.connect(write=True) as db:
            task = db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing'").fetchone()
            payload = json.loads(task['payload']); payload['blocked_by'] = value
            db.execute("UPDATE daily_work_tasks SET status='waiting',payload=? WHERE id=?", (dumps(payload), task['id']))
    block(PRINT_REVIEW_BLOCK)
    assert engine.assert_reprint_access(actor, ['S1'], 'P')
    block(LABEL_REVIEW_BLOCK)
    with pytest.raises(DailyWorkConflict, match='业务异常'): engine.assert_reprint_access(actor, ['S1'], 'P')
    with pytest.raises(DailyWorkConflict, match='业务异常'): engine.assert_reprint_access(user('admin', [], True), ['S1'], 'P')


@pytest.mark.parametrize('field,value', [('warehouse_id','changed'), ('product_id',''), ('sku_id','changed'), ('base_unit','changed'), ('customer_name','另一个客户')])
def test_raw_identity_or_customer_change_blocks_prepared_claim_without_resend(app, field, value):
    jobs = app.extensions['service']['jobs']; plan = preview(app, ['ORDER-B']); job = prepare(app, plan)
    with jobs.store.connect(write=True) as db:
        item = json.loads(db.execute("SELECT payload FROM records WHERE id='line-b1'").fetchone()[0]); item[field] = value
        db.execute("UPDATE records SET payload=? WHERE id='line-b1'", (dumps(item),))
    with pytest.raises(JobConflict, match='身份'):
        jobs.claim(job['job_id'], {'user_initiated': True, 'device_id': 'phone-a', 'idempotency_key': uuid.uuid4().hex}, actor=ACTOR)
    assert jobs.get(job['job_id'])['status'] == 'cancelled'
    assert fact(app, 'ORDER-B')['state'] == 'unprinted'


def test_same_data_version_raw_identity_change_blocks_prepare(app):
    jobs = app.extensions['service']['jobs']; plan = preview(app, ['ORDER-B'])
    with jobs.store.connect(write=True) as db:
        item = json.loads(db.execute("SELECT payload FROM records WHERE id='line-b1'").fetchone()[0]); item['sku_id'] = 'other'
        db.execute("UPDATE records SET payload=? WHERE id='line-b1'", (dumps(item),))
    with pytest.raises(JobConflict, match='身份已变化'): prepare(app, plan)
    assert fact(app, 'ORDER-B')['state'] == 'unprinted'


@pytest.mark.parametrize('change', ['unassigned', 'inactive', 'role_removed', 'off_duty'])
def test_admin_cannot_reuse_preview_with_invalid_current_owner(store, change):
    seed(store, [daily_order('S1', 'LS0001', 'LS库存件')], {'S1': 'LS库存件'})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False); enable_assignment(engine); engine.refresh()
    with store.connect(write=True) as db:
        if change == 'unassigned': db.execute("UPDATE daily_work_tasks SET assignee_id='' WHERE role='stock_packing'")
        if change == 'inactive': db.execute("UPDATE staff_users SET active=0 WHERE id='packer'")
        if change == 'role_removed': db.execute("UPDATE staff_users SET roles=? WHERE id='packer'", (dumps(['arrival_packing']),))
        if change == 'off_duty': db.execute("INSERT INTO daily_work_attendance VALUES (?, 'packer',0,'admin','now')", (engine.today(),))
    with pytest.raises(DailyWorkConflict): engine.assert_reprint_access(user('admin', [], True), ['S1'], 'P')


def test_access_checker_is_called_once_for_whole_preview_not_per_order(app):
    jobs = app.extensions['service']['jobs']; calls = []
    jobs.order_access_checker = lambda body, items, db=None: calls.append([item['order_no'] for item in items])
    preview(app, ['ORDER-A', 'ORDER-B', 'ORDER-C'])
    assert len(calls) == 1 and len(calls[0]) == 4


from tests.test_task_board import board, publish
from core.print_jobs import PrintJobs
from core.queries import Queries

def test_real_board_print_selection_flows_into_preview_and_prepare(board):
    publish(board, [daily_order('ERP1', 'LS1', 'LS库存件'), daily_order('ERP2', 'LS1', 'LS库存件')])
    actor = user('packer', ['stock_packing']); actor['display_name'] = '包装甲'
    listing = board.list(actor, 'stock_packing', packing_mode='print', work_state='todo')
    selected = board.select(actor, {'role': 'stock_packing', 'view': 'mine', 'packing_mode': 'print', 'purpose': 'print',
        'ids': [item['id'] for item in listing['items']], 'data_version': listing['data_version'],
        'task_version': listing['task_version'], 'expected_print_state_version': listing['print_state_version']})
    def guard(body, items, db=None):
        board.daily.assert_reprint_access(actor, [item['order_no'] for item in items], 'P', db=db)
    jobs = PrintJobs(board.store, Queries(board.store), order_access_checker=guard)
    plan = jobs.preview({'selection_id': selected['selection_id'], 'device_id': 'phone-a',
                         'expected_data_version': listing['data_version']}, actor=actor)
    assert plan['order_count'] == 2 and plan['label_count'] == 2
    job = jobs.prepare({'kind': 'order', 'preview_id': plan['preview_id'], 'preview_hash': plan['preview_hash'],
        'segment_index': 1, 'device_id': 'phone-a', 'prepare_idempotency_key': uuid.uuid4().hex}, actor=actor)
    assert job['order_count'] == 2 and job['actor_name'] == '包装甲'
