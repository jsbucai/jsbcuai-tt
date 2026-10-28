"""Durable, explicitly submitted inventory batches; no ERP or hardware imports."""
import json
import os
import re
import threading
import uuid
from decimal import Decimal, InvalidOperation

from .print_jobs import JobConflict
from .store import dumps, timestamp


class AdjustmentConflict(JobConflict):
    def __init__(self, message):
        super().__init__(message, 'adjustment_conflict')


class AdjustmentFailure(Exception):
    """The sender knows this request did NOT create an ERP document.

    Never use this for a timeout or a disconnected response after submission:
    raise an ordinary exception instead, which is conservatively recorded unknown.
    """
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable is True


_registry_guard = threading.Lock()
_worker_locks = {}


def _identity(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label}必须为 UUID')
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f'{label}必须为 UUID') from exc


def _text(value, label, maximum):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > maximum or '\0' in value:
        raise ValueError(f'{label}格式无效或过长')
    return value.strip()


def _quantity(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('数量必须为正数，最多6位小数')
    raw = str(value).strip()
    if len(raw) > 64:
        raise ValueError('数量过长')
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError('数量必须为正数，最多6位小数') from exc
    if not number.is_finite() or number <= 0 or number > 100_000_000:
        raise ValueError('数量必须大于0且不超过100000000，最多6位小数')
    digits, exponent = list(number.as_tuple().digits), number.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if exponent < -6:
        raise ValueError('数量最多6位小数')
    # Avoid float conversion or Decimal.normalize()'s context-dependent rounding.
    result = format(number, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _payload(value):
    if not isinstance(value, dict):
        raise ValueError('批次必须为 JSON 对象')
    identity = _identity(value.get('id'), '批次标识')
    client_id = _identity(value.get('client_id'), '设备标识')
    kind = value.get('type')
    if kind not in {'loss', 'overflow'}:
        raise ValueError('类型必须为报损或报溢')
    source_items = value.get('items')
    if not isinstance(source_items, list) or not 1 <= len(source_items) <= 100:
        raise ValueError('每批请选择1–100个商品')
    items, seen = [], set()
    for source in source_items:
        if not isinstance(source, dict):
            raise ValueError('商品明细必须为 JSON 对象')
        sku = _text(source.get('sku'), '商品编号', 128).upper()
        if not sku or any(char.isspace() for char in sku):
            raise ValueError('商品编号无效')
        if sku in seen:
            raise ValueError('同一批次的商品编号不能重复，请先汇总数量')
        seen.add(sku)
        items.append({'sku': sku, 'name': _text(source.get('name'), '名称', 256),
                      'spec': _text(source.get('spec'), '规格', 256),
                      'model': _text(source.get('model'), '型号', 256),
                      'quantity': _quantity(source.get('quantity')),
                      'note': _text(source.get('note'), '备注', 200)})
    items.sort(key=lambda item: item['sku'])
    return {'id': identity, 'client_id': client_id, 'type': kind, 'items': items}


class AdjustmentJobs:
    def __init__(self, store, queries, sender=None):
        self.store, self.queries, self.sender = store, queries, sender
        self.thread = None
        self._lifecycle = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker_handle = None
        self._process_lock = None
        with store.connect(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS adjustment_batches(
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '', document_no TEXT NOT NULL DEFAULT '',
                    retryable INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS adjustment_client_status
                    ON adjustment_batches(client_id,status,updated_at);
                CREATE TABLE IF NOT EXISTS adjustment_confirmations(
                    batch_id TEXT PRIMARY KEY,client_id TEXT NOT NULL,
                    document_no TEXT NOT NULL,previous_status TEXT NOT NULL,
                    previous_message TEXT NOT NULL,confirmed_at TEXT NOT NULL);
            ''')
            if 'completion_source' not in {row['name'] for row in db.execute('PRAGMA table_info(adjustment_batches)')}:
                db.execute("ALTER TABLE adjustment_batches ADD COLUMN completion_source TEXT NOT NULL DEFAULT ''")

    def _result(self, row):
        return {**json.loads(row['payload']), 'status': row['status'],
                'message': row['message'], 'document_no': row['document_no'] or self._saved_reference(row['id']),
                'completion_source': row['completion_source'],
                'retryable': bool(row['retryable']), 'created_at': row['created_at'],
                'updated_at': row['updated_at']}

    def _saved_reference(self, identity):
        # An interrupted worker may have saved a receipt before recording its state.
        # Reading only this batch's marker restores its reference, not its outcome.
        for filename in ('submission.json', 'prepared.json'):
            path = self.store.runtime / 'adjustments' / identity / filename
            if not path.resolve().is_relative_to(self.store.runtime.resolve()):
                continue
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                number = saved.get('document_no', '')
                if saved.get('batch_id') == identity and isinstance(number, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}', number):
                    return number
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return ''

    @staticmethod
    def _owned(db, identity, client_id):
        row = db.execute('SELECT * FROM adjustment_batches WHERE id=? AND client_id=?',
                         (identity, client_id)).fetchone()
        if row is None:
            raise KeyError('此设备的库存批次不存在')
        return row

    def enqueue(self, payload):
        normalized = _payload(payload)
        frozen = dumps(normalized)
        with self.store.connect(write=True) as db:
            previous = db.execute('SELECT * FROM adjustment_batches WHERE id=?',
                                  (normalized['id'],)).fetchone()
            if previous is not None:
                if previous['payload'] != frozen:
                    raise AdjustmentConflict('该批次标识已用于其他内容，不能覆盖或重复上传')
                return self._result(previous)
            # The write transaction serializes against snapshot publication too.
            for item in normalized['items']:
                if not self.queries.inventory_lookup(item['sku']).get('found'):
                    raise ValueError(f"商品 {item['sku']} 不在当前库存数据中，请重新查询")
            now = timestamp()
            db.execute('''INSERT INTO adjustment_batches
                (id,client_id,payload,status,message,created_at,updated_at)
                VALUES (?,?,?,'queued',?,?,?)''',
                (normalized['id'], normalized['client_id'], frozen, '已汇总，等待统一上传', now, now))
            result = self._result(self._owned(db, normalized['id'], normalized['client_id']))
        self._wake.set()
        return result

    def get(self, identity, client_id):
        identity, client_id = _identity(identity, '批次标识'), _identity(client_id, '设备标识')
        with self.store.connect() as db:
            return self._result(self._owned(db, identity, client_id))

    def list(self, client_id):
        client_id = _identity(client_id, '设备标识')
        with self.store.connect() as db:
            # Never truncate unresolved batches: a hidden unknown must not be lost.
            rows = db.execute('''SELECT * FROM adjustment_batches
                WHERE client_id=? AND status!='succeeded' ORDER BY updated_at DESC,rowid DESC''',
                (client_id,)).fetchall()
        return [self._result(row) for row in rows]

    def status(self):
        with self.store.connect() as db:
            counts = {row['status']: row['count'] for row in db.execute(
                'SELECT status,COUNT(*) AS count FROM adjustment_batches GROUP BY status')}
        return {'worker_running': bool(self.thread and self.thread.is_alive()), 'counts': counts}

    def retry(self, identity, client_id):
        identity, client_id = _identity(identity, '批次标识'), _identity(client_id, '设备标识')
        with self.store.connect(write=True) as db:
            row = self._owned(db, identity, client_id)
            if row['status'] != 'failed' or not row['retryable']:
                raise AdjustmentConflict('仅明确未生成单据且允许重试的失败批次可以重试')
            db.execute('''UPDATE adjustment_batches SET status='queued',retryable=0,
                message=?,updated_at=? WHERE id=?''', ('已请求重试，等待统一上传', timestamp(), identity))
            result = self._result(self._owned(db, identity, client_id))
        self._wake.set()
        return result

    def confirm(self, identity, payload):
        if not isinstance(payload, dict) or payload.get('confirmed') is not True:
            raise ValueError('请先核对 ERP 单据及明细数量，再确认已入账')
        identity, client_id = _identity(identity, '批次标识'), _identity(payload.get('client_id'), '设备标识')
        with self.store.connect(write=True) as db:
            row = self._owned(db, identity, client_id)
            if row['status'] == 'succeeded':
                return self._result(row)  # Lost acknowledgements are safe to repeat.
            if row['status'] != 'unknown':
                raise AdjustmentConflict('仅待核对的清单可以人工确认；进行中的上传不能提前结束')
            number = row['document_no'] or self._saved_reference(identity)
            now = timestamp()
            db.execute('''INSERT INTO adjustment_confirmations
                (batch_id,client_id,document_no,previous_status,previous_message,confirmed_at)
                VALUES (?,?,?,?,?,?)''', (identity, client_id, number, row['status'], row['message'], now))
            db.execute('''UPDATE adjustment_batches SET status='succeeded',retryable=0,
                completion_source='user_verified',document_no=?,message=?,updated_at=? WHERE id=?''',
                (number, '已人工核对 ERP，确认本清单已入账', now, identity))
            return self._result(self._owned(db, identity, client_id))

    def _acquire_worker(self):
        key = str(self.store.runtime.resolve()).casefold()
        with _registry_guard:
            lock = _worker_locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            return False
        handle = None
        try:
            path = self.store.runtime / '.adjustments-worker.lock'
            if not path.resolve().is_relative_to(self.store.runtime.resolve()):
                raise ValueError('库存上传锁不能指向开发目录之外')
            handle = path.open('a+b')
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                lock.release()
                return False
            self._worker_handle, self._process_lock = handle, lock
            return True
        except BaseException:
            if handle is not None:
                handle.close()
            lock.release()
            raise

    def _release_worker(self):
        handle, lock = self._worker_handle, self._process_lock
        self._worker_handle = self._process_lock = None
        try:
            if handle is not None:
                handle.close()  # Closing also releases the OS-level byte/flock lock.
        finally:
            if lock is not None:
                lock.release()

    def start(self):
        """Explicitly start the sole uploader; return False if another owns it."""
        with self._lifecycle:
            if self.thread and self.thread.is_alive():
                return False
            if not callable(self.sender):
                raise ValueError('尚未配置库存批次上传通道')
            if not self._acquire_worker():
                return False
            try:
                with self.store.connect(write=True) as db:
                    db.execute('''UPDATE adjustment_batches SET status='unknown',retryable=0,
                        message=? || substr(id,1,8) || ?,updated_at=? WHERE status='running' ''',
                        ('批次 ', ' 上传时服务中断，请先核对 ERP 单据；不会自动重试', timestamp()))
                self._stop.clear()
                self._wake.set()
                self.thread = threading.Thread(target=self._loop, name='android-stock-batches', daemon=True)
                self.thread.start()
                return True
            except BaseException:
                self._release_worker()
                raise

    def stop(self, timeout=5):
        """Stop after the current submission; never cancel/reissue an in-flight send."""
        with self._lifecycle:
            self._stop.set()
            self._wake.set()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        return not thread or not thread.is_alive()

    def _take(self):
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM adjustment_batches WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE adjustment_batches SET status='running',message=?,retryable=0,updated_at=? WHERE id=? AND status='queued'",
                       ('正在统一上传，请勿重复提交', timestamp(), row['id']))
            return self._result(self._owned(db, row['id'], row['client_id']))

    def _finish(self, batch, status, message, document_no='', retryable=False):
        with self.store.connect(write=True) as db:
            db.execute('''UPDATE adjustment_batches SET status=?,message=?,document_no=?,retryable=?,updated_at=?,completion_source=?
                WHERE id=? AND status='running' ''',
                (status, message, document_no, int(retryable), timestamp(), 'erp_receipt' if status == 'succeeded' else '', batch['id']))

    @staticmethod
    def _unknown_details(batch, error, reason='上传结果未确认'):
        number = getattr(error, 'document_no', '')
        number = number.strip() if isinstance(number, str) else ''
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}', number):
            number = ''
        reference = f'单据 {number}' if number else f"批次 {batch['id'][:8]}"
        return number, f'{reference} {reason}，请先核对 ERP 单据；不会自动重试'

    def _send(self, batch):
        try:
            result = self.sender(batch)
            if not isinstance(result, dict):
                raise ValueError('上传结果格式无效')
            document_no = _text(result.get('document_no'), '单据号', 128)
            if not document_no:
                raise ValueError('上传结果缺少单据号')
            message = _text(result.get('message'), '上传说明', 500) or '上传成功'
        except AdjustmentFailure as error:
            self._finish(batch, 'failed', str(error)[:500], retryable=error.retryable)
        except Exception as error:
            number, message = self._unknown_details(batch, error)
            self._finish(batch, 'unknown', message, document_no=number)
        else:
            try:
                self._finish(batch, 'succeeded', message, document_no=document_no)
            except Exception as error:
                error.document_no = document_no
                raise

    def _loop(self):
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    batch = self._take()
                except Exception:
                    # A transient local database failure must not kill the sole
                    # worker. No sender has been called before a durable claim.
                    self._stop.wait(0.5)
                    continue
                if batch is None:
                    self._wake.wait(0.5)  # Also observes enqueues from another API instance.
                else:
                    try:
                        self._send(batch)
                    except Exception as error:
                        # ERP may already have accepted this batch. If recording
                        # its response fails, retry only the local unknown write.
                        number, message = self._unknown_details(batch, error, '上传结果未能保存')
                        while True:
                            try:
                                self._finish(batch, 'unknown', message, document_no=number)
                                break
                            except Exception:
                                if self._stop.wait(0.5):
                                    return  # Persistent running is recovered as unknown on next start.
        finally:
            self._release_worker()
