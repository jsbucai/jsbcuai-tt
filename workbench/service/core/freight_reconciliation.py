"""A freight success receipt is a bounded lease, not permanent ERP truth."""
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .logistics import CODES

ZONE = ZoneInfo('Asia/Shanghai')
RECHECK_AFTER = timedelta(hours=12)
RECHECK_BATCH = 250


def observation(row):
    company = row.get('freightcompany')
    # This ERP normally returns null for the category. Only explicitly known
    # category names count as a comparable observation, never null or an ID.
    name = company.strip() if isinstance(company, str) else ''
    return {'document_id': str(row.get('vchcode') or ''),
            'freightnumber': str(row.get('freightbillno') or ''),
            'observed_at': str(row.get('_observed_at') or ''),
            **({'freightname': name, 'freightcode': CODES[name]} if name in CODES else {})}


def stamp(value):
    if not value or len(str(value)) <= 10:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=ZONE) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def fingerprints(snapshot):
    """Bind acknowledgement to source identity and demand, excluding read clocks.

    Never include allocation, print history or current freight: our own write
    would otherwise invalidate its receipt on the next collection.
    """
    observations = snapshot.get('logistics_observations', {})
    result = {}
    keys = ('line_id', 'profile_id', 'warehouse_id', 'product_id', 'sku_id',
            'base_unit', 'open_qty')
    for order in snapshot['orders']:
        number = order['order_no']
        lines = [{k: str(line.get(k, '')) for k in keys} for line in order['lines']]
        body = {'order_no': number, 'order_date': order.get('order_date'),
                'delivery_date': order.get('delivery_date'),
                'document_id': observations.get(number, {}).get('document_id', ''),
                'lines': sorted(lines, key=lambda line: json.dumps(line, sort_keys=True))}
        result[number] = hashlib.sha256(json.dumps(body, sort_keys=True,
            ensure_ascii=True).encode()).hexdigest()
    return result


def recheck_reason(cached, proof, fingerprint, now):
    # A manual resolution may refresh the old cache without possessing a new
    # source fingerprint. Re-establish it through the normal guarded writer.
    if not proof or proof['confirmed_at'] != cached['updated_at']:
        return 'legacy_receipt'
    if proof['fingerprint'] != fingerprint:
        return 'source_changed'
    written = stamp(cached['updated_at'])
    if written is None or now - written >= RECHECK_AFTER or written > now + timedelta(minutes=5):
        return 'lease_expired'
    return ''


def recheck_priority(cached, proof):
    # Definitive failures move to the back of the sweep, so a broken old order
    # cannot starve later orders. Unknown outcomes stay held by UpdateJobs.
    attempted = stamp(proof['last_attempt_at']) if proof else None
    return attempted or stamp(cached['updated_at']) or datetime.min.replace(tzinfo=ZONE)
