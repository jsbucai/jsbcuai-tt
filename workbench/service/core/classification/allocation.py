"""Production rules extracted from NAS 20260924 baseline; no I/O or ERP writes."""
from __future__ import annotations
import math
import datetime as dt
from dataclasses import dataclass
from collections import defaultdict
EPSILON = 1e-7
CLASSIFICATION_VERSION = 4


def number(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}不是有效数量: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}不是有限数量")
    return result


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and value == int(value):
        return str(int(value))
    return str(value).strip()


@dataclass(frozen=True)
class Demand:
    order: str
    warehouse: str
    sku: str
    date: dt.date
    quantity: float
    source_rows: tuple[int, ...] = ()
    product_code: str = ""

    @property
    def key(self):
        return self.warehouse, self.sku


def inventory_category(sku: str) -> str:
    prefix = sku[:2].upper()
    return (prefix if prefix in {"LS", "LD", "LM", "DQ"} else "QT") + "库存件"


def allocate(demands: list[Demand], stock: dict, incoming: dict) -> list[dict]:
    totals = defaultdict(float)
    for item in demands:
        if item.quantity <= 0 or not math.isfinite(item.quantity):
            raise ValueError("订单未完成数量必须为正数")
        if item.key not in stock:
            raise ValueError(f"库存源未找到仓库/商品: {item.key}")
        totals[item.key] += item.quantity
    previous = defaultdict(float)
    output = []
    # Stable ties do not depend on the ERP's current display order.
    for item in sorted(demands, key=lambda d: (d.warehouse, d.sku, d.date, d.order)):
        physical = max(0.0, number(stock[item.key], "实物库存"))
        transit = number(incoming.get(item.key, 0), "采购未到数量")
        if transit < 0:
            raise ValueError("采购未到数量不能为负")
        prior = previous[item.key]
        actual = max(0.0, min(item.quantity, physical - prior))
        pending = max(0.0, min(item.quantity - actual, transit - max(0.0, prior - physical)))
        shortage = max(0.0, item.quantity - actual - pending)
        if shortage > EPSILON:
            category = "需采购"
        elif actual + EPSILON < item.quantity:
            category = "待到货"
        else:
            category = inventory_category(item.product_code or item.sku)
        output.append(dict(demand=item, stock=physical, incoming=transit, total=totals[item.key],
                           prior=prior, actual=actual, pending=pending, shortage=shortage, category=category))
        previous[item.key] += item.quantity
    assign_order_categories(output)
    return output


def assign_order_categories(output: list[dict]) -> None:
    categories = defaultdict(set)
    for row in output:
        categories[row["demand"].order].add(row["category"])
    for row in output:
        choices = categories[row["demand"].order]
        # A ready, aged line must not hide another line's uncovered shortage or
        # outstanding delivery. Packing warnings apply to fully supplied orders.
        row["order_category"] = next((c for c in ("需采购", "催货", "待到货", "异常", "来货件") if c in choices),
                                     next(iter(choices)) if len(choices) == 1 else "库存件")
