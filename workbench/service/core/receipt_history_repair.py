"""Audited recovery of legacy receipt provenance, never a category override.

Administrative maintenance only. Call under the shared update/browser lane;
the transaction rejects an active update, stale scope or any unproven target.
No print, inventory balance, human follow-up or ERP table is written here.
"""
import copy
import hashlib
import json
from datetime import datetime

from .classification.receipts import observed_time, previous_orders


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def require(value, message):
    if not value:
        raise ValueError(message)


def equal(a, b):
    return abs(float(a) - float(b)) < 1e-7


def verify_proof(key, event, part, before, transition, later):
    """Three saved observations prove an unchanged shortage filled by this lot.

    The event may have been omitted by an old source adapter (internal output),
    or posted during a stock/receipt read race. A later unrelated replenishment
    and pre-history guesses deliberately cannot pass this proof.
    """
    identity = json.loads(key)
    b, t, l = (x['snapshot'] for x in (before, transition, later))
    bs, ts, ls = (x['state'] for x in (b, t, l))
    ba, ta = b['allocation'], t['allocation']
    for item in (b, t, l):
        d = item['allocation']['demand']
        require([d['order'], d['warehouse'], d['sku'], d['date']] == identity,
                '历史观察不属于同一订单和库存身份')
    eid = event['id']
    require((event['warehouse'], event['sku']) == tuple(identity[1:3]) and
            event['date'] >= identity[3] and event['quantity'] > 0 and
            event['type'] in {'进货入库单', '内部组装／拆卸'}, '来货明细身份无效')
    posted = observed_time(event.get('posted_at'))
    bt, tt, lt = (observed_time(x['calculated_at']) for x in
                  (before['last'], transition['first'], later['first']))
    require(posted and bt and tt and lt and bt < posted <= tt < lt,
            '入库不在缺货至齐货的历史观察区间内')
    require(bs['actual'] < bs['quantity'] and
            equal(bs['quantity'], ts['quantity']) and
            equal(ts['quantity'], ls['quantity']) and
            equal(ts['actual'], ts['quantity']) and ts['route'] == 'inventory',
            '缺少相同需求从不足转为库存齐货的证据')
    require(not b['events'].get(eid) and not t['events'].get(eid),
            '该批次当时已被来源适配器收录，不适用迟到纠正')
    saved_event = l['events'].get(eid) or {}
    require(all(saved_event.get(k) == event.get(k) for k in
                ('id','document','warehouse','sku','date','type','quantity')) and
            (not saved_event.get('posted_at') or
             observed_time(saved_event['posted_at']) == posted),
            '后来收录的入库明细发生变化')
    found = [p for p in ls['receipts'] if p['id'] == eid and p['arrival'] is False]
    require(found == [part] and equal(part['quantity'], ts['quantity'] - bs['actual'])
            and equal(ta['stock'] - ba['stock'], event['quantity']),
            '实际补缺数量或库存增量与入库不一致')


def repair(db, manifest, load_observation, *, dry_run=False):
    """Caller supplies a dedicated SQLite connection, never an active transaction.

    load_observation(db, job_id, key, event_id) must read the saved calculation,
    not trust copied evidence in the proposed manifest. Entire batch is atomic.
    """
    digest = hashlib.sha256(encode(manifest).encode()).hexdigest()
    require(manifest.get('id') and manifest.get('targets'), '纠正批次为空')
    db.execute('BEGIN IMMEDIATE')
    try:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_provenance_corrections'").fetchone()
        if exists:
            old = db.execute('SELECT payload FROM receipt_provenance_corrections WHERE id=?', (manifest['id'],)).fetchone()
            if old:
                result = json.loads(old[0])
                require(result.get('manifest_sha256') == digest, '同一纠正批次不能变更范围')
                db.rollback()
                return dict(result, already_applied=True)
        require(not db.execute("SELECT 1 FROM update_jobs WHERE status IN ('queued','running')").fetchone(), '更新正在运行，稍后重试')
        ledger = json.loads(db.execute("SELECT value FROM settings WHERE key='receipt_ledger_v1'").fetchone()[0])
        previous_orders(ledger)
        changed, seen = [], set()
        # Validate every target and historical proof before changing any entry.
        for target in manifest['targets']:
            key, eid = target['ledger_key'], target['event']['id']
            require((key, eid) not in seen, '重复纠正目标')
            seen.add((key, eid))
            identity = json.loads(key)
            entry = ledger['orders'].get(key)
            require(entry == target['expected'], '订单来源版本已改变：' + identity[0])
            require(ledger['events'].get(eid) == target['event'], '入库事件版本已改变')
            scope = json.loads(identity[1])
            rows = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM records WHERE kind='orders' AND json_extract(payload,'$.order_no')=?", (identity[0],))]
            active = [r for r in rows if
                      [str(r.get(n, '')) for n in ('profile_id','warehouse_id','sku_id','base_unit')] == scope
                      and str(r.get('product_id')) == identity[2]]
            require(active and all(not r.get('shadow_demand') and
                    r.get('lifecycle_state') == 'active' and
                    not r.get('lifecycle_pending_review') for r in active),
                    '当前有效未交身份不一致：' + identity[0])
            observations = [load_observation(db, target[n], key, eid)
                            for n in ('before_job', 'transition_job', 'later_job')]
            verify_proof(key, target['event'], target['part'], *observations)
            require(target['part'] in entry['receipts'] and
                    entry['route'] == 'inventory', '当前错误来源分配已改变')
        for target in manifest['targets']:
            key, eid = target['ledger_key'], target['event']['id']
            entry = ledger['orders'][key]
            before = copy.deepcopy(entry)
            part = next(p for p in entry['receipts'] if p == target['part'])
            part['arrival'] = True
            claims = {p['id']:p['quantity'] for p in entry.get('arrival_claims', [])}
            claims[eid] = max(claims.get(eid, 0), part['quantity'])
            entry['arrival_claims'] = [{'id':k,'quantity':v} for k,v in claims.items()]
            entry['route'] = 'arrival'
            entry.pop('inventory_ready_observed_at', None)
            entry.pop('inventory_ready_receipt_covered_through', None)
            changed.append({'ledger_key':key,'event_id':eid,'before':before,
                            'after':copy.deepcopy(entry),'product_code':target['product_code'],
                            'proof_jobs':[target[n] for n in ('before_job','transition_job','later_job')]})
        previous_orders(ledger)
        result = {'id':manifest['id'], 'manifest_sha256':digest,
                  'at':datetime.now().isoformat(), 'changed_count':len(changed),
                  'changed':changed, 'dry_run':dry_run}
        if dry_run:
            db.rollback()
            return result
        db.execute('CREATE TABLE IF NOT EXISTS receipt_provenance_corrections(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,payload TEXT NOT NULL)')
        db.execute("UPDATE settings SET value=? WHERE key='receipt_ledger_v1'", (encode(ledger),))
        db.execute('INSERT INTO receipt_provenance_corrections VALUES (?,?,?)',
                   (manifest['id'], result['at'], encode(result)))
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
