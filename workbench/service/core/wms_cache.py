"""Sanitized durable WMS details used to avoid repeated full-year downloads."""
import json
from datetime import datetime

from .store import dumps


HEAD_FIELDS = {
    'vchcode', 'number', 'tradeid', 'sourceid', 'ktypeid', 'deliverstatus',
    'freightbillno', 'freightcompany', 'summary', 'comment', 'createtime', 'modifytime',
    # Business convention confirmed 2026-09-30: receiveraddress stores the
    # customer/company name for UL orders.  Other receiver PII stays excluded.
    'receiveraddress',
}
LINE_FIELDS = {
    'id', 'sourcedetailid', 'ptypeid', 'skuid', 'usercode', 'unit', 'unitrate',
    'qty', 'assqty', 'unitname',
    # Private only: these never enter the generic order/stock response. Old
    # rows without amounts are not supplemented from guessed query prices.
    'total', 'price', 'assprice', 'tax', 'ktypeid',
}


def sanitized(row):
    result = {key: row.get(key) for key in HEAD_FIELDS if key in row}
    goods = row.get('goodslist')
    result['goodslist'] = [
        {key: line.get(key) for key in LINE_FIELDS if key in line}
        for line in goods
    ] if isinstance(goods, list) else []
    return result


class WmsCache:
    def __init__(self, store):
        self.store = store

    def metadata(self, profile):
        with self.store.connect() as database:
            row = database.execute(
                "SELECT payload FROM erp_sync_watermarks WHERE kind='wms-orders-v1' AND profile=?",
                (str(profile),),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    def get(self, profile, tradeids):
        wanted = {str(value) for value in tradeids}
        if not wanted:
            return {}
        output = {}
        with self.store.connect() as database:
            for row in database.execute(
                "SELECT tradeid,payload,updated_at FROM erp_wms_cache WHERE profile=?", (str(profile),)
            ):
                if row['tradeid'] in wanted:
                    try:
                        payload = json.loads(row['payload'])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict):
                        # This is the actual source-read time, not the time a
                        # later sync happens to reuse this cached document.
                        payload['_observed_at'] = row['updated_at']
                        output[row['tradeid']] = payload
        return output

    def order_dates(self, profile, tradeids):
        wanted = {str(value) for value in tradeids}
        if not wanted:
            return {}
        output = {}
        with self.store.connect() as database:
            for row in database.execute(
                "SELECT tradeid,order_date FROM erp_order_date_cache WHERE profile=?",
                (str(profile),),
            ):
                if row['tradeid'] in wanted:
                    output[row['tradeid']] = row['order_date']
        return output

    def put_order_dates(self, profile, values, source, updated_at):
        stamp = updated_at.isoformat(timespec='seconds') if isinstance(updated_at, datetime) \
            else str(updated_at)
        with self.store.connect(write=True) as database:
            for tradeid, order_date in values.items():
                existing = database.execute(
                    "SELECT order_date FROM erp_order_date_cache WHERE profile=? AND tradeid=?",
                    (str(profile), str(tradeid)),
                ).fetchone()
                if existing and existing[0] != str(order_date):
                    raise ValueError('WMS 订单日期与已验证缓存冲突')
                database.execute(
                    "INSERT OR IGNORE INTO erp_order_date_cache VALUES(?,?,?,?,?)",
                    (str(profile), str(tradeid), str(order_date), str(source), stamp),
                )

    def commit(self, profile, requested, rows, synced_at, full=False):
        profile = str(profile)
        wanted = {str(value) for value in requested}
        selected, ambiguous = {}, set()
        for raw in rows:
            tradeid = str(raw.get('tradeid') or '').strip()
            # Keep every pending WMS order because Qimen's returned open-order
            # set can fluctuate slightly between otherwise valid reads.
            if tradeid not in wanted and str(raw.get('deliverstatus')) != '0':
                continue
            value = sanitized(raw)
            if tradeid in selected and dumps(selected[tradeid]) != dumps(value):
                if tradeid in wanted:
                    raise ValueError('WMS 同一订单号出现不同内容')
                ambiguous.add(tradeid)
                selected.pop(tradeid, None)
                continue
            if tradeid in ambiguous:
                continue
            selected[tradeid] = value
        stamp = synced_at.isoformat(timespec='seconds') if isinstance(synced_at, datetime) else str(synced_at)
        previous = self.metadata(profile) or {}
        with self.store.connect(write=True) as database:
            if full:
                database.execute("DELETE FROM erp_wms_cache WHERE profile=?", (profile,))
            for tradeid, payload in selected.items():
                database.execute(
                    "INSERT OR REPLACE INTO erp_wms_cache VALUES(?,?,?,?)",
                    (profile, tradeid, dumps(payload), stamp),
                )
            metadata = {
                'last_sync': stamp,
                'last_full': stamp if full else previous.get('last_full', ''),
                'full_year': synced_at.year if full and isinstance(synced_at, datetime)
                    else previous.get('full_year'),
            }
            database.execute(
                "INSERT OR REPLACE INTO erp_sync_watermarks VALUES('wms-orders-v1',?,?,?)",
                (profile, dumps(metadata), stamp),
            )
        return selected
