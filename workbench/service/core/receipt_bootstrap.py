"""Convert the verified Windows receipt ledger to the API identity contract."""
import copy
import json

from .classification.receipts import previous_orders
from .receipt_identity import ledger_warehouse, receipt_event_id


def text(value):
    return "" if value is None else str(value).strip()


def convert_receipt_state(raw_state, mapping, inventory_rows, profile_id,
                          warehouse_id):
    if raw_state.get("version") != 1 or raw_state.get("dispatch_version") != 1:
        raise ValueError("旧入库账本版本不受支持")
    if not isinstance(raw_state.get("events"), dict) or not isinstance(raw_state.get("balances"), dict):
        raise ValueError("旧入库账本结构不完整")
    if mapping.get("matched_events") != len(raw_state["events"]) or mapping.get("unmatched_events") != 0:
        raise ValueError("旧入库事件未全部完成 API 身份映射")

    profile_id, warehouse_id = text(profile_id), text(warehouse_id)
    inventory_by_ids = {}
    inventory_by_code = {}
    for row in inventory_rows:
        if text(row.get("profile_id")) != profile_id or text(row.get("warehouse_id")) != warehouse_id:
            continue
        identity = tuple(text(row.get(name)) for name in
            ("product_id", "sku_id", "base_unit"))
        if any(not value for value in identity):
            continue
        inventory_by_ids.setdefault(identity[:2], []).append(identity)
        code = text(row.get("sku")).upper()
        if code:
            inventory_by_code.setdefault(code, []).append(identity)

    mapped = {text(row.get("legacy_event_id")): row for row in mapping.get("mappings", [])}
    if len(mapped) != len(raw_state["events"]):
        raise ValueError("账本映射存在缺失或重复旧事件")

    events, event_ids, used_inventory = {}, {}, set()
    for old_id, old_event in raw_state["events"].items():
        row = mapped.get(old_id)
        if row is None or row.get("quantity_equal") is not True:
            raise ValueError("旧入库事件数量未与 API 闭合")
        dimensions = {
            (text(item.get("warehouse_id")), text(item.get("product_id")), text(item.get("sku_id")))
            for item in row.get("api_identities", [])
        }
        if len(dimensions) != 1:
            raise ValueError("旧入库事件对应多个 API 商品身份")
        event_warehouse, product_id, sku_id = next(iter(dimensions))
        if event_warehouse != warehouse_id:
            raise ValueError("旧入库事件包含业务仓以外记录")
        candidates = inventory_by_ids.get((product_id, sku_id), [])
        if len(candidates) != 1:
            raise ValueError("旧入库事件无法唯一关联当前库存身份")
        _, _, base_unit = candidates[0]
        posted_date = text(old_event.get("date"))
        new_id = receipt_event_id(profile_id, old_event.get("document"),
            old_event.get("type"), warehouse_id, product_id, sku_id,
            base_unit, posted_date)
        if new_id in events:
            raise ValueError("转换后的入库事件身份重复")
        ledger_key = ledger_warehouse(profile_id, warehouse_id, sku_id, base_unit)
        events[new_id] = {
            "id": new_id,
            "document": text(old_event.get("document")),
            "type": text(old_event.get("type")),
            "warehouse": ledger_key,
            "sku": product_id,
            "date": posted_date,
            "quantity": float(old_event.get("quantity")),
        }
        event_ids[old_id] = new_id
        used_inventory.add((profile_id, warehouse_id, product_id, sku_id, base_unit))

    balances = {}
    for old_id, quantity in raw_state["balances"].items():
        if old_id not in event_ids:
            raise ValueError("旧入库余额引用未知事件")
        balances[event_ids[old_id]] = float(quantity)

    orders = {}
    for encoded, item in raw_state.get("orders", {}).items():
        identity = json.loads(encoded)
        if not isinstance(identity, list) or len(identity) != 4:
            raise ValueError("旧订单分配身份结构无效")
        order_no, _old_warehouse, sku_code, order_date = map(text, identity)
        candidates = inventory_by_code.get(sku_code.upper(), [])
        if len(candidates) != 1:
            raise ValueError("旧订单商品编码无法唯一关联当前库存身份")
        product_id, sku_id, base_unit = candidates[0]
        ledger_key = ledger_warehouse(profile_id, warehouse_id, sku_id, base_unit)
        new_key = json.dumps([order_no, ledger_key, product_id, order_date], ensure_ascii=False)
        if new_key in orders:
            raise ValueError("转换后的订单分配身份重复")
        converted = copy.deepcopy(item)
        for receipt in converted.get("receipts", []):
            old_event_id = text(receipt.get("id"))
            if old_event_id not in event_ids:
                raise ValueError("订单分配引用未映射入库事件")
            receipt["id"] = event_ids[old_event_id]
        orders[new_key] = converted
        used_inventory.add((profile_id, warehouse_id, product_id, sku_id, base_unit))

    period_start = text(raw_state.get("period_start"))
    period_end = text(raw_state.get("period_end"))
    if not period_start or not period_end or period_start > period_end:
        raise ValueError("旧入库账本时间范围无效")
    state = {
        "version": 1,
        "dispatch_version": 1,
        "profile_id": profile_id,
        "period_start": period_start,
        "period_end": period_end,
        "events": events,
        "balances": balances,
        "orders": orders,
        "scope_periods": {
            json.dumps(list(key), ensure_ascii=False): [period_start, period_end]
            for key in sorted(used_inventory)
        },
    }
    previous_orders(state)
    return state
