"""Report current order-date and inventory-identity gaps without customer fields."""
import json
import os
import sys
from collections import Counter
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.erp_api import ErpClient
from core.erp_cache import ReferenceCache
from core.erp_source import LiveSource, api_day, collection, expanded, number, text, variant_id
from core.store import Store
from core.wms_cache import WmsCache


def main():
    runtime = Path(os.environ.get("UL_PREFLIGHT_RUNTIME", "/app/service/runtime/data"))
    store = Store(runtime)
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    reference = ReferenceCache(store).get("catalog-v1", profile)
    if not reference:
        raise RuntimeError("catalog cache missing")
    products = collection(reference.get("products"), "products")
    warehouses = collection(reference.get("warehouses"), "warehouses")
    product_map = {text(row["ptypeid"]): row for row in products}
    code_map = {}
    for product in products:
        for variant in product.get("skus") or []:
            code = text(variant.get("usercode") or variant.get("xcode") or product.get("usercode")).upper()
            if code:
                code_map.setdefault(code, []).append((product, variant))

    source = LiveSource(client, reference_cache=ReferenceCache(store), wms_cache=WmsCache(store))
    orders = source._orders(profile)
    requested = {text(row["trade_order"]["trade_id"]): row for row in orders}
    details = WmsCache(store).get(profile, requested)
    dates = WmsCache(store).order_dates(profile, requested)
    missing_dates = []
    identity_gaps = []
    warehouse_names = {text(row.get("fullname")): text(row.get("id")) for row in warehouses}
    for order_no, raw in requested.items():
        head = raw["trade_order"]
        if order_no not in dates:
            missing_dates.append({"order_no": order_no, "pay_date": api_day(head.get("pay_time")),
                                  "wms_cached": order_no in details})
        detail = details.get(order_no, {})
        ext = expanded(head.get("extendProps"))
        header_warehouse = text(detail.get("ktypeid") or ext.get("k_type_id") or head.get("warehouse_id"))
        if not header_warehouse:
            header_warehouse = warehouse_names.get(text(head.get("warehouse_name")), "")
        for item in collection(raw.get("order_info"), "order_info"):
            code = text(item.get("spec_code") or item.get("goods_code")).upper()
            reported = number(item["sell_count"])
            catalog = code_map.get(code, [])
            matching_lines = [row for row in detail.get("goodslist", [])
                              if text(row.get("usercode")).upper() == code and
                              number(row.get("assqty", row.get("qty"))) == reported]
            verified = len(matching_lines) == 1
            line = matching_lines[0] if verified else {}
            if verified:
                product_id, sku_id = text(line.get("ptypeid")), text(line.get("skuid"))
                verified = product_id in product_map and any(
                    variant_id(value) == sku_id for value in product_map[product_id].get("skus") or [])
            unit_verified = verified and text(line.get("unit")) == "1" and number(line.get("unitrate")) == "1"
            line_warehouse = text(line.get("ktypeid") or detail.get("ktypeid")) if verified else ""
            if not (verified and unit_verified and line_warehouse):
                identity_gaps.append({
                    "order_no": order_no,
                    "product_code": code,
                    "catalog_matches": len(catalog),
                    "wms_cached": bool(detail),
                    "wms_line_matches": len(matching_lines),
                    "wms_line_count": len(detail.get("goodslist", [])),
                    "header_warehouse": header_warehouse,
                    "line_warehouse": line_warehouse,
                    "unit": text(line.get("unit")),
                    "unitrate": text(line.get("unitrate")),
                    "qimen_unit": text(item.get("goods_unit")),
                })
    print(json.dumps({
        "qimen_unique_orders": len(requested),
        "wms_cache_matches": len(details),
        "missing_order_dates": len(missing_dates),
        "missing_date_pay_days": dict(sorted(Counter(
            row["pay_date"] or "(empty)" for row in missing_dates).items())),
        "missing_date_examples": missing_dates[:12],
        "identity_gaps": identity_gaps,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
