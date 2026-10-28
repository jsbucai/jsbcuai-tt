"""Local platform order numbers and line numbers. Nothing is sent to ERP.

The order prefix is a code constant. Change PLATFORM_ORDER_PREFIX to retarget
new automatic numbers; already saved numbers are left as written.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import re

from .purchase_workspace_data import text

PLATFORM_ORDER_PREFIX = 'CG'
_LINE_RE = re.compile(r'[0-9]+')


def shanghai_now():
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def _cell(row, name, index):
    return row[name] if hasattr(row, 'keys') else row[index]


def next_platform_line_no(db, order_no):
    """Next numeric line on this order. Non-numeric legacy values are ignored.

    Cancelled rows still count, so a number is not reused. The caller must
    already hold the store write transaction.
    """
    rows = db.execute(
        """SELECT json_extract(details,'$.platform_line_no') AS line_no
           FROM purchase_workspace_allocations
           WHERE json_extract(details,'$.platform_order_no')=?""",
        (order_no,)).fetchall()
    maximum = 0
    for row in rows:
        raw = text(_cell(row, 'line_no', 0))
        if _LINE_RE.fullmatch(raw):
            maximum = max(maximum, int(raw))
    return str(maximum + 1)


def next_auto_order_no(db, now=None, prefix=PLATFORM_ORDER_PREFIX):
    """CG + Shanghai date + sequence. Manual numbers are not a sequence, but block reuse."""
    moment = now or shanghai_now()
    if moment.tzinfo is None:
        raise ValueError('自动订单号的时间必须包含时区')
    day = moment.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')
    head = f'{prefix}{day}'
    rows = db.execute(
        """SELECT json_extract(details,'$.platform_order_no') AS no,
                  json_extract(details,'$.order_no_auto') AS auto
           FROM purchase_workspace_allocations
           WHERE json_extract(details,'$.platform_order_no') LIKE ?""",
        (head + '%',)).fetchall()
    taken = set()
    maximum = 0
    pattern = re.compile(rf'^{re.escape(head)}(\d+)$')
    for row in rows:
        number = text(_cell(row, 'no', 0))
        auto = _cell(row, 'auto', 1)
        taken.add(number)
        match = pattern.fullmatch(number)
        if match and str(auto) in {'1', '1.0', 'true'}:
            maximum = max(maximum, int(match.group(1)))
    for _ in range(1000):
        maximum += 1
        candidate = f'{head}{maximum:03d}'
        if candidate not in taken:
            return candidate
    raise ValueError('当天自动订单号已用尽，请手工填写订单号')
