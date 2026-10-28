"""Validate or install reviewed historical order delivery dates.

Input may be either a JSON object mapping full order numbers to ``YYYY-MM-DD``
or a JSON list whose rows contain ``order_no`` and ``delivery_date``.  The
default mode is validation only; ``--apply`` is required to write SQLite.
"""
import argparse
import json
from pathlib import Path

from core.order_lifecycle import import_delivery_dates
from core.store import Store


def mapping(path):
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if isinstance(value, dict):
        result = value
    elif isinstance(value, list):
        result = {}
        for row in value:
            if not isinstance(row, dict):
                raise ValueError('交期导入列表只允许 JSON 对象')
            number = str(row.get('order_no') or '').strip()
            day = str(row.get('delivery_date') or '').strip()
            if not number or number in result:
                raise ValueError('交期导入存在空订单号或重复订单号')
            result[number] = day
    else:
        raise ValueError('交期导入文件必须是 JSON 对象或列表')
    return {str(number).strip(): str(day).strip() for number, day in result.items()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--runtime', type=Path, default=Path('runtime/data'))
    parser.add_argument('--source', default='verified_historical_import')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    values = mapping(args.input)
    # Validation uses the same strict date/order contract but a disposable
    # result only.  No production database is opened until --apply is explicit.
    if args.apply:
        result = import_delivery_dates(
            Store(args.runtime), args.profile_id, values, source=args.source)
        result['outcome'] = 'installed'
    else:
        from datetime import date
        if not str(args.profile_id).strip() or not str(args.source).strip():
            raise ValueError('交期导入缺少账套或来源')
        for number, day in values.items():
            if not number:
                raise ValueError('交期导入缺少完整订单号')
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError('订单交期必须使用 YYYY-MM-DD')
        result = {'profile_id': str(args.profile_id).strip(),
                  'validated': len(values), 'installed': 0,
                  'source': str(args.source).strip(), 'outcome': 'validated'}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
