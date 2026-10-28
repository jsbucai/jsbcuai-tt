"""Explicit, document-bound delivery dates. Never infer dates for new orders."""
import hashlib
import json
from datetime import date, datetime

from .rules import parse_summary, text


def ensure_schema(store):
    with store.connect(write=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS order_delivery_confirmations(
            profile_id TEXT NOT NULL, order_no TEXT NOT NULL,
            document_id TEXT NOT NULL, order_date TEXT NOT NULL,
            identity_hash TEXT NOT NULL, delivery_date TEXT NOT NULL,
            batch_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(profile_id,order_no,document_id))''')


def day(value):
    value = text(value)
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('交期确认必须使用完整年月日')
    return value


def document_identity(details):
    """Bind to the ERP document and its actual goods, excluding mutable freight."""
    goods = details.get('goodslist')
    if not text(details.get('vchcode')) or not isinstance(goods, list) or not goods:
        raise ValueError('交期确认缺少 ERP 单据身份')
    lines = []
    for line in goods:
        keys = ('id', 'sourcedetailid', 'ptypeid', 'skuid', 'usercode', 'unit')
        row = [text(line.get(k)) for k in keys]
        if not row[2] or not row[4]:
            raise ValueError('交期确认缺少商品身份')
        lines.append(row)
    identity = [text(details.get('vchcode')), text(details.get('tradeid')),
                text(details.get('ktypeid')), text(details.get('receiveraddress')),
                sorted(lines)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def load(store, profile):
    with store.connect() as db:
        rows = db.execute('SELECT * FROM order_delivery_confirmations WHERE profile_id=?',
                          (str(profile),)).fetchall()
    return {json.dumps([r['order_no'], r['document_id']]): dict(r) for r in rows}


def resolve(confirmations, number, order_date, details):
    row = confirmations.get(json.dumps([number, text(details.get('vchcode'))]))
    if not row or row['order_date'] != order_date:
        return None
    try:
        if row['identity_hash'] != document_identity(details):
            return None
        # An explicit ERP date (including an invalid/ambiguous one) wins.
        if parse_summary(details.get('summary'))['delivery_date_status'] != 'missing':
            return None
        return {'delivery_date': day(row['delivery_date']),
                'delivery_date_source': 'user_confirmed_batch',
                'delivery_date_confirmation_id': row['batch_id']}
    except (ValueError, TypeError):
        return None


def install(store, profile, rows, batch_id, reason):
    """Atomically install a reviewed list; retrying the same list is a no-op.

    Callers supply final dates, never a calculation rule. Current published
    demand, order date and WMS identity must still match the reviewed evidence.
    """
    if not rows or not text(profile) or not text(batch_id) or not text(reason):
        raise ValueError('交期确认缺少范围或来源')
    if len({r['order_no'] for r in rows}) != len(rows):
        raise ValueError('交期确认存在重复订单')
    ensure_schema(store)
    installed = 0
    with store.connect(write=True) as db:
        for row in rows:
            number = text(row['order_no'])
            order_date, delivery_date = day(row['order_date']), day(row['delivery_date'])
            lifecycle = db.execute('''SELECT lifecycle_state FROM order_lifecycle_state
                WHERE profile_id=? AND order_no=?''', (profile, number)).fetchone()
            if not lifecycle or lifecycle[0] != 'active':
                raise ValueError(f'{number} 已不在当前未交范围')
            saved = db.execute('''SELECT payload FROM erp_wms_cache
                WHERE profile=? AND tradeid=?''', (profile, number)).fetchone()
            details = json.loads(saved[0]) if saved else {}
            if (text(details.get('vchcode')) != row['document_id'] or
                    document_identity(details) != row['identity_hash'] or
                    parse_summary(details.get('summary'))['delivery_date_status'] != 'missing'):
                raise ValueError(f'{number} ERP 单据或交期已改变，请重新核对')
            published = db.execute('''SELECT last_business_snapshot FROM order_lifecycle_state
                WHERE profile_id=? AND order_no=?''', (profile, number)).fetchone()
            current = json.loads(published[0]).get('order', {})
            if current.get('order_date') != order_date:
                raise ValueError(f'{number} 下单日期已改变')
            prior = db.execute('''SELECT order_date,identity_hash,delivery_date,batch_id,reason
                FROM order_delivery_confirmations
                WHERE profile_id=? AND order_no=? AND document_id=?''',
                (profile, number, row['document_id'])).fetchone()
            values = (order_date, row['identity_hash'], delivery_date, batch_id, reason)
            if prior:
                if tuple(prior) != values:
                    raise ValueError(f'{number} 已有不同交期确认，拒绝覆盖')
                continue
            if current.get('delivery_date'):
                raise ValueError(f'{number} 已有交期，拒绝覆盖')
            db.execute('''INSERT INTO order_delivery_confirmations VALUES (?,?,?,?,?,?,?,?,?)''',
                (profile, number, row['document_id'], *values[:3], batch_id, reason,
                 datetime.now().astimezone().isoformat(timespec='seconds')))
            installed += 1
    return {'batch_id': batch_id, 'validated': len(rows), 'installed': installed}
