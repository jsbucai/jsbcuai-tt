"""Business refresh times; all comparisons use Asia/Shanghai."""
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')


def workday_slot(now=None):
    now = now or datetime.now(ZONE)
    if now.tzinfo is None:
        raise ValueError('自动更新时刻必须包含时区')
    now = now.astimezone(ZONE)
    if 8 <= now.hour < 19:
        return now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    if now.hour == 7 and now.minute >= 30:
        return now.replace(minute=30, second=0, microsecond=0)
    return None


def business_open(now=None):
    now = now or datetime.now(ZONE)
    return 8 <= now.astimezone(ZONE).hour < 19
