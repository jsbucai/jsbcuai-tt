"""Fast CPCL preparation and one-shot client delivery ledger. No hardware imports."""
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from .rules import ORDER_LOGISTICS, STOCK_LOGISTICS, normalized, text, split_packages, parse_note
from .fields import order_fields, order_total, inventory_fields
from .mobile_cpcl import fast_cpcl
from .store import dumps, timestamp
from .product_catalog import is_deleted
from .print_ordering import label_key, ordered_segments, sort_fields, catalog_sort_fields
from .order_print_state import (OrderStateConflict, current_orders, ensure_schema,
                                finish_orders, order_fingerprint, order_ref,
                                reserve_orders, reset_order, print_state_details)


class JobConflict(Exception):
    def __init__(self, message, code='print_conflict'):
        super().__init__(message)
        self.code = code


def data_version(store):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
        return json.loads(row[0]) if row else ''


def _database_data_version(db):
    """Read the published generation through the caller's transaction."""
    row = db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
    if not row:
        return ''
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return text(row[0])


def _flag(value):
    return value is True or value == 1 or text(value).lower() in {'1', 'true', 'yes', 'on'}


def _assert_printing_open(db):
    row = db.execute("SELECT value FROM settings WHERE key='printing_maintenance'").fetchone()
    if row and row[0] == 'true':
        raise JobConflict('打印正在维护，请等待管理员恢复后重新选择', 'printing_maintenance')


def _assert_not_revoked(row):
    if row['status'] == 'revoked':
        raise JobConflict('原打印任务已由管理员撤销，请刷新后重新选择；不会恢复旧发送', 'print_job_revoked')


def _lifecycle_blocks_printing(value):
    """Conservatively identify a lifecycle observation that forbids packaging."""
    state = text(value.get('lifecycle_state'))
    process_state = text(value.get('process_state'))
    return (state in {'candidate', 'shipped_unposted', 'archived'} or
            process_state in {'500', '600'} or bool(text(value.get('last_error'))))


def _source_identity_fingerprint(rows):
    values = [{key: text(row.get(key)) for key in
        ('id', 'line_id', 'profile_id', 'warehouse_id', 'product_id', 'sku_id',
         'base_unit', 'customer_name', 'buyer', 'customer_source')} for row in rows]
    values.sort(key=lambda row: (row['line_id'], row['id']))
    return hashlib.sha256(dumps(values).encode()).hexdigest()


def _validate_preview_sources(groups):
    for ref, rows in groups.items():
        if (not rows or any(not all(text(row.get(key)) for key in
                ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')) or
                row.get('identity_complete') is False or _flag(row.get('read_only')) or
                _flag(row.get('source_review')) for row in rows)):
            raise JobConflict(f'订单 {ref[1]} 商品身份或来源待核对，不能打印', 'print_identity_conflict')
        customers = {text(row.get('customer_name') or row.get('buyer'))
                     if row.get('customer_source') == 'wms_receiveraddress' else '' for row in rows}
        if len(customers) != 1 or '' in customers:
            raise JobConflict(f'订单 {ref[1]} 客户身份待核对，不能打印', 'print_identity_conflict')


def _validate_first_order_claim(db, meta):
    """Validate a frozen order immediately before its bytes can leave N150.

    The caller owns a ``BEGIN IMMEDIATE`` transaction.  This serializes the
    check with snapshot/lifecycle publication, closing the prepare-to-claim
    window without weakening the one-shot contract once bytes may have left.
    """
    orders = meta.get('orders') if isinstance(meta.get('orders'), list) else []
    refs = {(text(value.get('profile_id')), text(value.get('order_no')))
            for value in orders if isinstance(value, dict) and text(value.get('order_no'))}
    if not refs:
        raise JobConflict('打印任务缺少完整订单范围，请重新查询后准备',
                          'order_lifecycle_conflict')

    found = set()
    groups = {}
    for row in db.execute("SELECT payload FROM records WHERE kind='orders'"):
        try:
            item = json.loads(row['payload'])
        except (TypeError, ValueError):
            continue
        ref = (text(item.get('profile_id')), text(item.get('order_no')))
        if ref not in refs:
            continue
        found.add(ref)
        groups.setdefault(ref, []).append(item)
        if (_flag(item.get('shadow_demand')) or
                _flag(item.get('lifecycle_print_blocked')) or
                _lifecycle_blocks_printing(item) or
                text(item.get('logistics_company')) == '已完成订单需结存'):
            raise JobConflict(f'订单 {ref[1]} 已完成发货或需结存，不能领取包装标签',
                              'order_lifecycle_conflict')

    missing = refs - found
    if missing:
        number = sorted(ref[1] for ref in missing)[0]
        raise JobConflict(f'订单 {number} 已不在当前待发数据，请刷新订单',
                          'order_lifecycle_conflict')

    if meta.get('verified_print_preview'):
        _validate_preview_sources(groups)
    for value in orders:
        ref = (text(value.get('profile_id')), text(value.get('order_no')))
        if (text(value.get('source_identity_fingerprint')) and
                _source_identity_fingerprint(groups.get(ref, [])) != value['source_identity_fingerprint']):
            raise JobConflict(f'订单 {ref[1]} 商品或客户身份已变化，不能领取旧标签', 'print_identity_conflict')
        if (meta.get('verified_print_preview') and text(value.get('fingerprint')) and
                order_fingerprint(groups.get(ref, [])) != value['fingerprint']):
            raise JobConflict(f'订单 {ref[1]} 标签内容已变化，不能领取旧标签', 'print_preview_conflict')

    table = db.execute("""SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='order_lifecycle_state'""").fetchone()
    if not table:
        return
    for ref in refs:
        row = db.execute('''SELECT * FROM order_lifecycle_state
            WHERE profile_id=? AND order_no=?''', ref).fetchone()
        if row and _lifecycle_blocks_printing(dict(row)):
            raise JobConflict(f'订单 {ref[1]} 已完成发货、需结存或状态待核对，不能领取包装标签',
                              'order_lifecycle_conflict')


def print_readiness(kind, item):
    """Missing label content blocks new preparations, never an existing frozen job."""
    if kind == 'inventory' and is_deleted(item):
        return False, '商品已在 ERP 删除，请刷新库存后选择当前商品'
    if item.get('printable') is False or item.get('print_blocked') is True:
        return False, text(item.get('print_block_reason')) or '该记录缺少打印必需字段'
    if kind == 'function_codes':
        return True, ''
    required = ('sku', 'product_name') if kind == 'inventory' else ('order_no', 'sku', 'product_name')
    if any(not text(item.get(key)) for key in required):
        return False, '缺少订单编号、商品编号或商品名称'
    if kind == 'order':
        if order_total(item) <= 0:
            return False, '订单打印数量必须为正数'
        # Summary and delivery warnings do not invalidate actual line facts.
    return True, ''


class PrintJobs:
    def __init__(self, store, queries, order_access_checker=None):
        self.store, self.queries = store, queries
        self.order_access_checker = order_access_checker
        ensure_schema(store)
        # ``Store`` owns the versioned schema migration.  Do not repair this
        # column here: doing so on an existing database would bypass the
        # consistent pre-migration backup required for every schema change.
        with store.connect() as db:
            columns = {row['name'] for row in db.execute('PRAGMA table_info(print_jobs)')}
            if 'prepare_idempotency_key' not in columns:
                raise RuntimeError('打印任务数据库迁移未完成，缺少准备幂等字段')

    @staticmethod
    def _prepare_replay(row, device_id, request_fingerprint):
        _assert_not_revoked(row)
        meta = json.loads(row['metadata'])
        if row['status'] != 'prepared':
            raise JobConflict('该准备操作已经领取或结束，不能重新准备',
                              'print_prepare_conflict')
        if (meta.get('device_id') != device_id or
                meta.get('prepare_request_fingerprint') != request_fingerprint):
            raise JobConflict('准备操作标识已用于不同设备或不同打印范围',
                              'print_prepare_idempotency_conflict')
        return {**json.loads(row['frozen']), **meta, 'status': 'prepared', 'replayed': True}

    @staticmethod
    def _actor(actor):
        actor = actor if isinstance(actor, dict) else {}
        return {'id': text(actor.get('id')), 'display_name': text(actor.get('display_name') or actor.get('username'))}

    @staticmethod
    def _preview_hash(value):
        return hashlib.sha256(dumps(value).encode()).hexdigest()

    @staticmethod
    def _order_rows(db, refs):
        groups = {}
        for row in db.execute("SELECT payload FROM records WHERE kind='orders'"):
            item = json.loads(row['payload'])
            if order_ref(item) in refs:
                groups.setdefault(order_ref(item), []).append(item)
        enriched = catalog_sort_fields(db, [item for rows in groups.values() for item in rows])
        groups = {}
        for item in enriched: groups.setdefault(order_ref(item), []).append(item)
        return groups

    def preview(self, payload, actor=None):
        actor = self._actor(actor)
        device = text(payload.get('device_id'))
        if not device or len(device) > 128:
            raise ValueError('预览打印需提供设备标识')
        with self.store.connect(write=True) as db:
            _assert_printing_open(db)
            row = db.execute('SELECT * FROM daily_work_selections WHERE id=?',
                             (text(payload.get('selection_id')),)).fetchone()
            if not row or row['actor_id'] != actor['id'] or row['role'] not in {'stock_packing','arrival_packing'}:
                raise JobConflict('打印选择不存在或属于其他人员，请重新选择', 'print_selection_conflict')
            selection = json.loads(row['payload'])
            if selection.get('purpose') != 'print':
                raise JobConflict('该选择不支持打印预览，请升级并重新选择', 'print_selection_conflict')
            version = _database_data_version(db)
            if not payload.get('expected_data_version') or payload['expected_data_version'] != version or row['data_version'] != version:
                raise JobConflict('业务数据已更新，请重新选择', 'data_version_conflict')
            task_version = db.execute("SELECT value FROM daily_work_settings WHERE key='task_version'").fetchone()
            if not task_version or int(task_version[0]) != row['task_version']:
                raise JobConflict('工作清单已变化，请重新选择', 'print_selection_conflict')
            numbers = selection.get('order_nos')
            profile = text(selection.get('profile_id'))
            if not isinstance(numbers, list) or not numbers or any(not text(n) for n in numbers) or len(set(numbers)) != len(numbers):
                raise JobConflict('选择缺少完整订单范围，请重新选择', 'print_selection_conflict')
            refs = {(profile, text(n)) for n in numbers}
            objects = selection.get('objects')
            if not isinstance(objects, list) or not objects:
                raise JobConflict('选择缺少任务版本，请重新选择', 'print_selection_conflict')
            tasks = []
            task_refs = set()
            for obj in objects:
                task = db.execute('SELECT * FROM daily_work_tasks WHERE id=?', (text(obj.get('id')),)).fetchone()
                if (obj.get('kind') != 'task' or not task or task['version'] != obj.get('version') or task['role'] != row['role']):
                    raise JobConflict('所选任务已变化，请重新选择', 'print_selection_conflict')
                task_refs.add((task['profile_id'], task['order_no']))
                tasks.append({'id': task['id'], 'version': task['version'], 'profile_id': task['profile_id'], 'order_no': task['order_no'], 'arrival_sources':json.loads(task['payload'] if 'payload' in task.keys() else '{}').get('arrival_sources',[])})
            if task_refs != refs:
                raise JobConflict('选择任务与整单范围不一致，请重新选择', 'print_selection_conflict')
            groups = self._order_rows(db, refs)
            if set(groups) != refs:
                raise JobConflict('所选订单已不在当前数据，请刷新', 'order_lifecycle_conflict')
            _validate_first_order_claim(db, {'verified_print_preview': True,
                'orders': [{'profile_id': p, 'order_no': n} for p, n in refs]})
            facts = print_state_details(db, refs)
            maximum = payload.get('max_per_package')
            if self.order_access_checker is not None:
                self.order_access_checker({'verified_print_preview': True},
                    [item for rows in groups.values() for item in rows], db)
            orders, segments = [], []
            for number in numbers:
                ref = (profile, text(number)); rows = groups[ref]
                for item in rows:
                    ready, reason = print_readiness('order', item)
                    if not ready:
                        raise JobConflict(reason, 'print_data_incomplete')
                fact = facts[ref]
                if fact['state'] == 'reserved':
                    who = fact['current_print_actor_name'] or text(fact.get('device_id')) or '另一台设备'
                    raise JobConflict(f'订单 {number} 正由 {who} 打印，不能重复占用', 'order_print_conflict')
                labels = sum(len(split_packages(order_total(item), maximum)) for item in rows)
                if labels > 100:
                    raise ValueError(f'整单 {number} 拆箱后超过100张，请增大每箱数量后重新预览；未拆开整单')
                orders.append({'profile_id': profile, 'order_no': number, 'print_state': fact['state'],
                    'state_revision': fact['state_revision'], 'label_fingerprint': order_fingerprint(rows),
                    'latest_print_actor_id': fact['latest_print_actor_id'],
                    'latest_print_actor_name': fact['latest_print_actor_name'], 'latest_print_at': fact['latest_print_at'],
                    'label_count': labels, 'line_count': len(rows),
                    'arrival_sources': [r for task in tasks if task['order_no']==number for r in task.get('arrival_sources',[])],
                    'lines': [{'line_id': text(item.get('line_id')), 'item_id': text(item.get('id')),
                               'sort_fields': sort_fields(item),
                               'sku': text(item.get('sku')), 'quantity': order_total(item),
                               'label_count': len(split_packages(order_total(item), maximum)),
                               'fields': order_fields(item), 'print_warnings': item.get('print_warnings', [])}
                              for item in sorted(rows, key=label_key)],
                    'source_identity_fingerprint': _source_identity_fingerprint(rows)})
            orders, segments = ordered_segments(orders, groups)
            identity = uuid.uuid4().hex
            value = {'preview_id': identity, 'selection_id': row['id'], 'actor_id': actor['id'],
                'device_id': device, 'data_version': version, 'profile_id': profile,
                'max_per_package': maximum, 'tasks': tasks, 'orders': orders, 'segments': segments,
                'print_sort': 'product_name_spec_natural_v1',
                'order_count': len(orders), 'line_count': sum(o['line_count'] for o in orders),
                'label_count': sum(o['label_count'] for o in orders),
                'reprint_count': sum(o['print_state'] == 'printed' for o in orders),
                'uncertain_count': sum(o['print_state'] == 'uncertain' for o in orders)}
            preview_hash = self._preview_hash(value)
            db.execute('INSERT INTO print_job_previews VALUES (?,?,?,?,?)',
                       (identity, actor['id'], device, dumps({'plan': value, 'hash': preview_hash, 'used': {}}), timestamp()))
            return {**value, 'preview_hash': preview_hash}

    def _validate_preview_segment(self, db, preview, segment):
        row = db.execute('SELECT * FROM print_job_previews WHERE id=?', (preview['plan']['preview_id'],)).fetchone()
        current = json.loads(row['payload_json']) if row else {}
        if current.get('hash') != preview['hash'] or str(segment['index']) in current.get('used', {}):
            raise JobConflict('这段打印预览已使用，请重新选择核对', 'print_preview_used')
        refs = {(preview['plan']['profile_id'], number) for number in segment['order_nos']}
        groups = self._order_rows(db, refs)
        if set(groups) != refs:
            raise JobConflict('所选订单已变化，请重新预览', 'order_lifecycle_conflict')
        if preview['plan'].get('print_sort') == 'product_name_spec_natural_v1':
            for order in preview['plan']['orders']:
                ref = (order['profile_id'], order['order_no'])
                if ref not in refs: continue
                actual = {text(item.get('id')): sort_fields(item) for item in groups[ref]}
                if actual != {line['item_id']: line['sort_fields'] for line in order['lines']}:
                    raise JobConflict('商品名称或规格已变化，请重新预览排序', 'print_preview_conflict')
        _validate_preview_sources(groups)
        for task in preview['plan']['tasks']:
            if (task['profile_id'], task['order_no']) not in refs:
                continue
            value = db.execute('SELECT * FROM daily_work_tasks WHERE id=?', (task['id'],)).fetchone()
            if not value or value['version'] != task['version'] or value['condition_state'] != 'active':
                raise JobConflict('该段工作事项已变化，请重新预览确认', 'print_selection_conflict')
        for fact in preview['plan']['orders']:
            ref = (fact['profile_id'], fact['order_no'])
            if ref in refs and _source_identity_fingerprint(groups[ref]) != fact['source_identity_fingerprint']:
                raise JobConflict(f"订单 {ref[1]} 商品或客户身份已变化，请重新预览", 'print_identity_conflict')
            if ref in refs and order_fingerprint(groups[ref]) != fact['label_fingerprint']:
                raise JobConflict(f"订单 {ref[1]} 标签内容已变化，请重新预览", 'print_preview_conflict')
        return current

    def _prepare_preview(self, payload, actor):
        if payload.get('kind', 'order') != 'order':
            raise ValueError('整单预览仅支持订单标签')
        if set(payload) - {'kind', 'preview_id', 'preview_hash', 'segment_index', 'device_id',
                           'prepare_idempotency_key', 'confirm_reprint', 'confirm_uncertain'}:
            raise ValueError('已确认预览不接受更改订单或拆箱范围')
        actor = self._actor(actor)
        if not text(payload.get('prepare_idempotency_key')):
            raise ValueError('预览打印需提供准备操作标识')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM print_job_previews WHERE id=?', (text(payload.get('preview_id')),)).fetchone()
            if not row or row['actor_id'] != actor['id'] or row['device_id'] != text(payload.get('device_id')):
                raise JobConflict('打印预览属于其他人员或设备', 'print_preview_conflict')
            preview = json.loads(row['payload_json'])
        if payload.get('preview_hash') != preview['hash']:
            raise JobConflict('打印预览已变化，请重新确认', 'print_preview_conflict')
        index = payload.get('segment_index')
        if type(index) is not int:
            raise ValueError('缺少打印分段编号')
        segment = next((s for s in preview['plan']['segments'] if s['index'] == index), None)
        if not segment:
            raise ValueError('打印分段不存在')
        facts = [f for f in preview['plan']['orders'] if f['order_no'] in segment['order_nos']]
        if any(f['print_state'] == 'printed' for f in facts) and payload.get('confirm_reprint') is not True:
            raise JobConflict('包含已打印订单，请明确确认重打', 'print_confirmation_required')
        if any(f['print_state'] == 'uncertain' for f in facts) and payload.get('confirm_uncertain') is not True:
            raise JobConflict('包含打印待核对订单，请检查纸张并明确确认重打', 'print_confirmation_required')
        trusted = {'kind': 'order', 'order_nos': segment['order_nos'], 'profile_id': preview['plan']['profile_id'],
            'device_id': row['device_id'], 'expected_data_version': preview['plan']['data_version'],
            'max_per_package': preview['plan']['max_per_package'], 'prepare_idempotency_key': payload['prepare_idempotency_key'],
            'verified_print_preview': True}
        return self.prepare(trusted, actor=actor, _preview=(preview, segment), _request_payload=payload)

    def prepare(self, payload, actor=None, _preview=None, _request_payload=None):
        if payload.get("preview_id") and _preview is None:
            return self._prepare_preview(payload, actor)
        if _preview is None and payload.get("verified_print_preview"):
            raise ValueError("重打必须先预览确认")
        actor = self._actor(actor)
        kind = payload.get('kind', 'order')
        if kind not in {'order', 'inventory', 'function_codes'}:
            raise ValueError('标签类型无效')
        prepare_key = text(payload.get('prepare_idempotency_key'))
        if prepare_key and not 16 <= len(prepare_key) <= 128:
            raise ValueError('准备打印操作标识长度无效')
        device_for_prepare = text(payload.get('device_id'))
        fingerprint_payload = {key: value for key, value in (_request_payload or payload).items()
                               if key != 'prepare_idempotency_key'}
        request_fingerprint = hashlib.sha256(json.dumps(
            fingerprint_payload, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        if prepare_key:
            with self.store.connect() as db:
                replay = db.execute('''SELECT * FROM print_jobs
                    WHERE prepare_idempotency_key=?''', (prepare_key,)).fetchone()
                if replay:
                    return self._prepare_replay(
                        replay, device_for_prepare or ('legacy' if kind == 'order' else ''),
                        request_fingerprint)
        with self.store.read_snapshot():
            version = data_version(self.store)
            if kind != 'function_codes':
                if not payload.get('expected_data_version'):
                    raise ValueError('缺少查询数据版本，请重新查询后打印')
                if payload['expected_data_version'] != version:
                    raise JobConflict('业务数据已更新，请重新查询并核对后打印', 'data_version_conflict')
            items = self.select(kind, payload)
            if kind == 'order' and _preview and _preview[0]['plan'].get('print_sort') == 'product_name_spec_natural_v1':
                with self.store.connect() as db: items = catalog_sort_fields(db, items)
                items = sorted(items, key=label_key)
            if kind == 'order' and self.order_access_checker is not None:
                self.order_access_checker(payload, items, None)
            pages = []
            for item in items:
                if kind == 'function_codes':
                    jobs = [({}, {})]
                elif kind == 'inventory':
                    jobs = [(inventory_fields(item), {})]
                else:
                    maximum = payload.get('max_per_package')
                    quantities = split_packages(order_total(item), maximum) if maximum not in (None, '', 0, '0') else [order_total(item)]
                    jobs = []
                    for index, amount in enumerate(quantities):
                        package = {'totalPackages': len(quantities), 'packageIndex': index + 1, 'packageQuantity': amount}
                        jobs.append((order_fields(item, package), package))
                for fields, package in jobs:
                    content = fast_cpcl(kind, fields)
                    pages.append({'index': len(pages) + 1, 'cpcl_base64': base64.b64encode(content).decode(), 'bytes': len(content), 'fields': fields, 'packageInfo': package,
                                  'line_ref': {key: text(item.get(key)) for key in ('profile_id', 'order_no', 'line_id', 'id')}})
                    if len(pages) > 100:
                        raise ValueError('拆箱后超过100张，请缩小范围分批打印')
        identity, now = uuid.uuid4().hex, timestamp()
        device_id = text(payload.get('device_id'))
        if len(device_id) > 128:
            raise ValueError('设备标识过长')
        if kind == 'order' and not device_id:
            device_id = 'legacy'
        if kind == 'order':
            selected_groups = {}
            for item in items:
                selected_groups.setdefault(order_ref(item), []).append(item)
            orders = [{'profile_id': ref[0], 'order_no': ref[1],
                       'fingerprint': order_fingerprint(rows),
                       **({'source_identity_fingerprint': _source_identity_fingerprint(rows)} if _preview else {})}
                      for ref, rows in selected_groups.items()]
        else:
            orders = []
        cpcl = b''.join(base64.b64decode(page['cpcl_base64']) for page in pages)
        frozen = {'job_id': identity, 'status': 'prepared', 'kind': kind, 'labels': len(pages), 'pages': pages, 'cpcl_pages': [page['cpcl_base64'] for page in pages], 'cpcl_base64': base64.b64encode(cpcl).decode(), 'cpcl_bytes': len(cpcl), 'bytes': len(cpcl), 'mobile_render_mode': 'text', 'paper_width_mm': 50, 'paper_height_mm': 100, 'data_version': version, 'sha256': hashlib.sha256(cpcl).hexdigest(), 'sent': False, 'printed': False, 'device_id': device_id, 'order_count': len(orders), 'orders': orders, 'query': payload.get('query') if isinstance(payload.get('query'), dict) else {}}
        meta = {key: value for key, value in frozen.items() if key not in {'pages', 'cpcl_pages', 'cpcl_base64'}}
        frozen['line_count'] = len(items) if kind == 'order' else 0
        meta['line_count'] = frozen['line_count']
        meta.update(created_at=now, updated_at=now, message='快速打印指令已准备，尚未发送',
                    prepare_idempotency_key=prepare_key,
                    prepare_request_fingerprint=request_fingerprint, actor_id=actor['id'],
                    actor_name=actor['display_name'])
        if _preview:
            meta.update(preview_id=_preview[0]['plan']['preview_id'],
                        preview_hash=_preview[0]['hash'], segment_index=_preview[1]['index'],
                        verified_print_preview=True)
        try:
            with self.store.connect(write=True) as db:
                _assert_printing_open(db)
                if prepare_key:
                    replay = db.execute('''SELECT * FROM print_jobs
                        WHERE prepare_idempotency_key=?''', (prepare_key,)).fetchone()
                    if replay:
                        return self._prepare_replay(replay, device_id, request_fingerprint)
                if kind != 'function_codes':
                    row = db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
                    current_version = json.loads(row[0]) if row else ''
                    if current_version != version:
                        raise JobConflict('业务数据已更新，请重新查询并核对后打印', 'data_version_conflict')
                if kind == 'order':
                    if self.order_access_checker is not None:
                        self.order_access_checker(payload, items, db)
                    expected = None
                    if _preview:
                        current = self._validate_preview_segment(db, *_preview)
                        expected = {(f['profile_id'], f['order_no']): f for f in _preview[0]['plan']['orders']}
                    reserve_orders(db, orders, device_id, identity, actor=actor, expected=expected)
                    if _preview:
                        current['used'][str(_preview[1]['index'])] = identity
                        db.execute('UPDATE print_job_previews SET payload_json=? WHERE id=?',
                                   (dumps(current), _preview[0]['plan']['preview_id']))
                db.execute('''INSERT INTO print_jobs
                    (id,status,frozen,metadata,prepare_idempotency_key) VALUES (?,?,?,?,?)''',
                    (identity, 'prepared', dumps(frozen), dumps(meta), prepare_key or None))
        except OrderStateConflict as exc:
            raise JobConflict(str(exc), 'order_print_conflict') from None
        return {**frozen, **meta, 'replayed': False}

    def select(self, kind, payload):
        if kind == 'function_codes':
            return [{}]
        query = payload.get('query') or {}
        if not isinstance(query, dict):
            raise ValueError('查询范围格式无效')
        scope = payload.get('scope') or query.get('scope') or 'all'
        company = text(payload.get('logistics_company') or query.get('logistics_company'))
        if (scope not in {'all', 'stock'} or
                (company and company not in ORDER_LOGISTICS) or
                (scope == 'stock' and company and company not in STOCK_LOGISTICS)):
            raise ValueError('查询范围或物流公司无效')
        if payload.get('all') is True:
            if kind == 'inventory':
                has_query = any(normalized(query.get(key)) for key in ('q', 'sku', 'name', 'spec', 'model', 'material', 'status'))
                has_query = has_query or text(query.get('only_available')).lower() in {'1', 'true', 'yes', 'on'}
            else:
                has_query = bool(text(query.get('q') or query.get('sku') or query.get('buyer') or
                                      query.get('summary') or query.get('note')))
                has_query = has_query or query.get('scope') == 'stock' or bool(text(query.get('logistics_company')))
                has_query = has_query or 'print_state' in query or any(text(query.get(key)) for key in
                    ('order_date_from', 'order_date_to', 'delivery_date_from', 'delivery_date_to'))
            if not has_query:
                raise ValueError('批量打印必须包含当前查询条件')
            args = {**query, 'scope': scope, 'logistics_company': company, 'page': 1, 'page_size': 100}
            result = self.queries.orders(args) if kind == 'order' else self.queries.inventory(args)
            if result['count'] > 100:
                raise ValueError('当前查询超过100条，请选择部分订单或缩小范围；未截断打印')
            items = result['items']
        else:
            order_nos = payload.get('order_nos')
            if order_nos is None and payload.get('order_no') is not None:
                order_nos = [payload.get('order_no')]
            if kind == 'order' and order_nos is not None:
                if (not isinstance(order_nos, list) or not 1 <= len(order_nos) <= 100 or
                        any(not text(value) for value in order_nos)):
                    raise ValueError('请选择1–100张完整订单')
                if len(set(map(text, order_nos))) != len(order_nos):
                    raise ValueError('打印列表含重复订单，请重新选择')
                requested = set(map(text, order_nos))
                profile = text(payload.get('profile_id'))
                candidates = [item for item in self.store.rows('orders')
                              if text(item.get('order_no')) in requested and
                              (not profile or text(item.get('profile_id')) == profile)]
                found_orders = {text(item.get('order_no')) for item in candidates}
                if found_orders != requested:
                    raise JobConflict('所选订单已变化，请重新查询', 'data_version_conflict')
                if not profile:
                    for number in requested:
                        if len({text(item.get('profile_id')) for item in candidates if text(item.get('order_no')) == number}) > 1:
                            raise ValueError('订单编号存在于多个账套，请提供 profile_id')
                items = candidates
            else:
                identities = payload.get('item_ids')
                if not isinstance(identities, list) or not 1 <= len(identities) <= 100 or any(not isinstance(identity, str) for identity in identities):
                    raise ValueError('请选择1–100条查询结果')
                if len(set(identities)) != len(identities):
                    raise ValueError('打印列表含重复记录，请重新选择')
                record_kind = 'orders' if kind == 'order' else 'inventory'
                with self.store.connect() as db:
                    rows = db.execute('SELECT id,payload FROM records WHERE kind=? AND id IN (' + ','.join('?' for _ in identities) + ')', [record_kind, *identities]).fetchall()
                found = {row['id']: json.loads(row['payload']) for row in rows}
                if len(found) != len(identities):
                    raise JobConflict('所选记录已变化，请重新查询', 'data_version_conflict')
                items = [found[identity] for identity in identities]
        if not items:
            raise ValueError('当前范围没有可打印数据')
        if kind == 'order':
            ordered_refs = list(dict.fromkeys(order_ref(item) for item in items))
            groups, _orders = current_orders(self.store, set(ordered_refs))
            items = [item for ref in ordered_refs for item in sorted(groups.get(ref, []), key=lambda row: (text(row.get('line_id')), text(row.get('id'))))]
        if kind == 'order' and any(scope == 'stock' and item.get('logistics_company') not in STOCK_LOGISTICS or company and item.get('logistics_company') != company for item in items):
            raise ValueError('所选订单不属于当前物流分类范围')
        for item in items:
            ready, reason = print_readiness(kind, item)
            if not ready:
                raise JobConflict(reason + '，请更新数据后重新选择', 'print_data_incomplete')
        return items

    @staticmethod
    def row(db, identity):
        row = db.execute('SELECT * FROM print_jobs WHERE id=?', (identity,)).fetchone()
        if not row:
            raise KeyError('打印任务不存在')
        return row

    def get(self, identity):
        with self.store.connect() as db:
            row = self.row(db, identity)
            return {**json.loads(row['metadata']), 'status': row['status'], 'replayed': False}

    def cancel(self, identity, payload):
        """Cancel a prepared job before any claim token can be issued.

        The state check, ownership check, reservation release, and job update
        share one ``BEGIN IMMEDIATE`` transaction. Claimed or result-bearing
        jobs must use the original claim token and can never be downgraded here.
        """
        key = text(payload.get('idempotency_key'))
        reason = text(payload.get('reason'))[:500]
        device_id = text(payload.get('device_id'))
        if not 16 <= len(key) <= 128:
            raise ValueError('取消打印需提供有效操作标识')
        if not reason:
            raise ValueError('取消打印需提供原因')
        if len(device_id) > 128:
            raise ValueError('设备标识过长')
        try:
            with self.store.connect(write=True) as db:
                row = self.row(db, identity)
                _assert_not_revoked(row)
                meta = json.loads(row['metadata'])
                if (row['status'] == 'cancelled' and
                        meta.get('cancel_idempotency_key') == key):
                    return {**meta, 'status': 'cancelled', 'replayed': True}
                if row['status'] == 'claimed':
                    raise JobConflict(
                        '打印任务已经领取；只能使用原领取令牌报告取消或发送结果',
                        'print_already_claimed')
                if (row['status'] != 'prepared' or row['claim_token_hash'] or
                        row['idempotency_key'] or
                        meta.get('sent') is not False or meta.get('printed') is not False or
                        meta.get('client_reported') or meta.get('bytes_sent') not in (None, 0)):
                    raise JobConflict('只有尚未领取且明确未发送的打印任务可以取消',
                                      'print_cancel_conflict')
                if (meta.get('kind') == 'order' and
                        meta.get('device_id') not in {'', 'legacy'} and
                        device_id != meta.get('device_id')):
                    raise JobConflict('打印任务属于另一台设备', 'order_print_conflict')
                if meta.get('kind') == 'order':
                    if self.order_access_checker is not None:
                        self.order_access_checker({'verified_print_preview': bool(meta.get('verified_print_preview'))}, meta.get('orders') or [], db)
                    finish_orders(db, meta.get('orders') or [], meta.get('device_id', ''),
                                  identity, 'cancelled', actor={'id': meta.get('actor_id'), 'display_name': meta.get('actor_name')})
                now = timestamp()
                meta.update(status='cancelled', updated_at=now, sent=False, printed=False,
                            client_reported=True, bytes_sent=0,
                            client_message=reason,
                            cancel_idempotency_key=key,
                            message='打印尚未领取或发送，任务已取消并释放订单')
                db.execute('''UPDATE print_jobs SET status='cancelled',metadata=?,idempotency_key=?
                    WHERE id=?''', (dumps(meta), key, identity))
                return {**meta, 'replayed': False}
        except OrderStateConflict as exc:
            raise JobConflict(str(exc), 'order_print_conflict') from None

    def recover(self, identity, payload):
        """Conservatively close a claim whose response/token was lost.

        Matching the original device and claim idempotency key proves which
        client initiated the claim. Since the server cannot know whether bytes
        left the phone, recovery always becomes ``unknown``/``uncertain`` and
        never makes the order printable again.
        """
        key = text(payload.get('idempotency_key'))
        reason = text(payload.get('reason'))[:500]
        device_id = text(payload.get('device_id'))
        if not 16 <= len(key) <= 128:
            raise ValueError('恢复打印需提供原领取操作标识')
        if not reason:
            raise ValueError('恢复打印需提供原因')
        if len(device_id) > 128:
            raise ValueError('设备标识过长')
        try:
            with self.store.connect(write=True) as db:
                row = self.row(db, identity)
                _assert_not_revoked(row)
                meta = json.loads(row['metadata'])
                if (row['status'] == 'unknown' and
                        meta.get('recover_idempotency_key') == key):
                    return {**meta, 'status': 'unknown', 'replayed': True}
                if row['status'] == 'prepared':
                    raise JobConflict('打印任务尚未领取，请使用取消接口释放',
                                      'print_not_claimed')
                if (row['status'] != 'claimed' or not row['claim_token_hash'] or
                        row['idempotency_key'] != key):
                    raise JobConflict('打印恢复信息与原领取记录不一致',
                                      'print_recovery_conflict')
                if (meta.get('kind') == 'order' and
                        meta.get('device_id') not in {'', 'legacy'} and
                        device_id != meta.get('device_id')):
                    raise JobConflict('打印任务属于另一台设备', 'order_print_conflict')
                if meta.get('kind') == 'order':
                    finish_orders(db, meta.get('orders') or [], meta.get('device_id', ''),
                                  identity, 'unknown', actor={'id': meta.get('actor_id'), 'display_name': meta.get('actor_name')})
                now = timestamp()
                meta.update(status='unknown', updated_at=now, sent=None, printed=False,
                            client_reported=True, bytes_sent=None, client_message=reason,
                            recover_idempotency_key=key,
                            message='领取结果无法恢复，已转为待核对；不会自动重新打印')
                db.execute("UPDATE print_jobs SET status='unknown',metadata=? WHERE id=?",
                           (dumps(meta), identity))
                return {**meta, 'replayed': False}
        except OrderStateConflict as exc:
            raise JobConflict(str(exc), 'order_print_conflict') from None

    def claim(self, identity, payload, actor=None):
        server_actor = self._actor(actor) if actor is not None else None
        key = text(payload.get('idempotency_key'))
        if payload.get('user_initiated') is not True or not 16 <= len(key) <= 128:
            raise ValueError('打印需由用户主动触发并提供有效操作标识')
        if payload.get('transport', 'native') not in {'native', 'bluetooth'}:
            raise ValueError('本服务仅提供原生App打印数据')
        rejected = None
        response = None
        with self.store.connect(write=True) as db:
            row = self.row(db, identity)
            _assert_not_revoked(row)
            _assert_printing_open(db)
            meta = json.loads(row['metadata'])
            request_device = text(payload.get('device_id'))
            if (server_actor is not None and meta.get('verified_print_preview') and
                    meta.get('actor_id') != server_actor['id']):
                raise JobConflict('打印预览属于其他人员，请重新选择', 'print_preview_conflict')
            if (meta.get('kind') == 'order' and meta.get('device_id') not in {'', 'legacy'} and
                    request_device != meta['device_id']):
                raise JobConflict('打印任务已由另一台设备准备', 'order_print_conflict')
            if row['idempotency_key']:
                if row['idempotency_key'] == key:
                    return {**meta, 'status': row['status'], 'replayed': True}
                raise JobConflict('该任务已领取，不会重复发送')
            if row['status'] != 'prepared':
                raise JobConflict(text(meta.get('claim_rejection_reason')) or
                                  '打印任务已结束，请重新查询后准备',
                                  text(meta.get('claim_rejection_code')) or 'print_conflict')
            if db.execute('SELECT 1 FROM print_jobs WHERE idempotency_key=?', (key,)).fetchone():
                raise JobConflict('该操作标识已用于另一批标签')
            try:
                if meta.get('kind') != 'function_codes':
                    if _database_data_version(db) != meta.get('data_version', ''):
                        raise JobConflict('业务数据已更新，请重新查询并核对后打印',
                                          'data_version_conflict')
                if meta.get('kind') == 'order':
                    _validate_first_order_claim(db, meta)
                    if self.order_access_checker is not None:
                        self.order_access_checker({'verified_print_preview': bool(meta.get('verified_print_preview'))}, meta.get('orders') or [], db)
            except JobConflict as exc:
                # No claim token has been issued, so the App cannot enter its
                # Bluetooth transfer step.  Cancel this stale prepared job and
                # release the whole-order reservation in the same transaction.
                if meta.get('kind') == 'order' and meta.get('orders'):
                    try:
                        finish_orders(db, meta['orders'], meta.get('device_id', ''),
                                      identity, 'cancelled', actor={'id': meta.get('actor_id'), 'display_name': meta.get('actor_name')})
                    except OrderStateConflict as state_exc:
                        raise JobConflict(str(state_exc), 'order_print_conflict') from None
                now = timestamp()
                meta.update(status='cancelled', updated_at=now, sent=False, printed=False,
                            message='订单或业务数据已变化，标签未发送，任务已取消',
                            claim_rejection_code=exc.code,
                            claim_rejection_reason=str(exc))
                db.execute('UPDATE print_jobs SET status=?,metadata=? WHERE id=?',
                           ('cancelled', dumps(meta), identity))
                rejected = exc
            else:
                token = secrets.token_urlsafe(32)
                if server_actor is not None:
                    meta.update(actor_id=server_actor['id'], actor_name=server_actor['display_name'])
                meta.update(status='claimed', updated_at=timestamp(), message='已领取数据，等待App发送结果', transport=payload.get('transport', 'native'))
                db.execute('UPDATE print_jobs SET status=?,metadata=?,idempotency_key=?,claim_token_hash=? WHERE id=?', ('claimed', dumps(meta), key, hashlib.sha256(token.encode()).hexdigest(), identity))
                response = {**json.loads(row['frozen']), **meta, 'claim_token': token,
                            'replayed': False}
        if rejected:
            raise rejected
        return response

    def result(self, identity, payload):
        status = payload.get('status')
        token, message = text(payload.get('claim_token')), text(payload.get('message'))[:2000]
        count = payload.get('bytes_sent')
        if status not in {'submitted', 'unknown', 'cancelled', 'failed'} or count is not None and (type(count) is not int or not 0 <= count <= 100_000_000):
            raise ValueError('打印结果格式无效')
        with self.store.connect(write=True) as db:
            row = self.row(db, identity)
            _assert_not_revoked(row)
            if not token or not row['claim_token_hash'] or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row['claim_token_hash']):
                raise JobConflict('打印结果令牌无效')
            meta = json.loads(row['metadata'])
            request_device = text(payload.get('device_id'))
            if (meta.get('kind') == 'order' and meta.get('device_id') not in {'', 'legacy'} and
                    request_device != meta['device_id']):
                raise JobConflict('打印任务属于另一台设备', 'order_print_conflict')
            if row['status'] != 'claimed':
                if row['status'] == status and meta.get('client_message') == message and meta.get('bytes_sent') == count:
                    return {**meta, 'replayed': True}
                raise JobConflict('结果已记录，不允许覆盖')
            try:
                if meta.get('kind') == 'order' and meta.get('orders'):
                    order_result = ('unknown' if status in {'cancelled', 'failed'} and
                                    (count != 0 if meta.get('verified_print_preview') else count not in {None, 0}) else status)
                    finish_orders(db, meta['orders'], meta.get('device_id', ''), identity, order_result,
                                  actor={'id': meta.get('actor_id'), 'display_name': meta.get('actor_name')})
            except OrderStateConflict as exc:
                raise JobConflict(str(exc), 'order_print_conflict') from None
            if status == 'submitted':
                result_message = 'App报告标签已全部提交'
            elif status in {'cancelled', 'failed'} and (count == 0 if meta.get('verified_print_preview') else count in {None, 0}):
                result_message = ('App报告未发送标签，已恢复原打印状态' if meta.get('verified_print_preview') and count == 0
                                  else 'App报告未发送标签，订单已恢复为未打印')
            else:
                result_message = '已记录发送结果；请先检查已出标签，不自动重发'
            meta.update(status=status, client_message=message, bytes_sent=count,
                        sent=True if status == 'submitted' else None,
                        printed=status == 'submitted', client_reported=True, updated_at=timestamp(),
                        message=result_message)
            db.execute('UPDATE print_jobs SET status=?,metadata=? WHERE id=?', (status, dumps(meta), identity))
            return {**meta, 'replayed': False}

    def reset_order_state(self, order_no, payload, access_checker=None, actor=None):
        device_id = text(payload.get('device_id'))
        if len(device_id) > 128:
            raise ValueError('设备标识过长')
        try:
            return reset_order(self.store, order_no, payload.get('profile_id'), device_id,
                               text(payload.get('reason'))[:500] or 'manual_reset',
                               access_checker=access_checker, actor=self._actor(actor),
                               idempotency_key=text(payload.get('idempotency_key')),
                               expected_state_revision=payload.get('expected_state_revision'),
                               expected_label_fingerprint=payload.get('expected_label_fingerprint'))
        except OrderStateConflict as exc:
            raise JobConflict(str(exc), 'order_print_conflict') from None
