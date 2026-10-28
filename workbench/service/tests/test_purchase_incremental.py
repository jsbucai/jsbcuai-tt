from datetime import datetime
from zoneinfo import ZoneInfo

from core.purchase_incremental import build_plan


def test_plan_uses_posting_window_and_skips_stockin():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
    empty = build_plan(None, now)
    assert empty['mode'] == 'full' and empty['capabilities']['erp.ewms.stockinbill.list']['used'] is False
    assert empty['capabilities']['erp.goodsstocks.list']['time_filter'] is False
    assert empty['capabilities']['erp.bill.billcenterlist']['time_filter'] is True
    watermark = datetime(2026, 10, 27, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
    plan = build_plan(watermark, now)
    assert plan['mode'] == 'incremental' and plan['begin'] == '2026-10-24 12:00:00'
    assert plan['orders'] == 'kept_until_full_reconcile'
