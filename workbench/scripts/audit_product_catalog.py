"""Read-only audit of product-master coverage and possible material fields."""
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, "/app/service")

from core.erp_api import ErpClient
from core.erp_source import END, START, text, variant_id


def present(value):
    return value not in (None, "", [], {})


def summarize_fields(rows, nested_key=None):
    counts = Counter()
    examples = defaultdict(list)
    values = []
    for row in rows:
        values.extend(row.get(nested_key) or [] if nested_key else [row])
    for row in values:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if isinstance(value, (dict, list)) or not present(value):
                continue
            counts[key] += 1
            shown = str(value).strip()
            if shown and shown not in examples[key] and len(examples[key]) < 5:
                examples[key].append(shown[:100])
    return {key: {"nonempty": counts[key], "examples": examples[key]}
            for key in sorted(counts)}


def main():
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    products = client.pages(
        "erp.ptype.list",
        {"begintime": START, "endtime": END, "timetype": 1, "isall": True},
        "ptypes",
        lambda row: text(row.get("ptypeid")),
        size=200,
        limit=2000,
    )

    catalog = {}
    invalid_variants = 0
    products_without_variants = 0
    for product in products:
        variants = product.get("skus") or []
        if not variants:
            products_without_variants += 1
        for variant in variants:
            sku_id = variant_id(variant)
            product_id = text(product.get("ptypeid"))
            if not product_id or not sku_id:
                invalid_variants += 1
                continue
            catalog[(product_id, sku_id)] = {
                "sku": text(variant.get("usercode") or variant.get("xcode") or product.get("usercode")).upper(),
                "stopped": bool(product.get("isstop")),
            }

    db_path = Path("/app/service/runtime/data/workbench.sqlite3")
    with sqlite3.connect(db_path) as db:
        stock_rows = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM records WHERE kind='inventory'")]
    target_warehouse = "29871293290508390"
    all_stock_keys = {(text(row.get("product_id")), text(row.get("sku_id"))) for row in stock_rows}
    target_stock_keys = {(text(row.get("product_id")), text(row.get("sku_id"))) for row in stock_rows
                         if text(row.get("warehouse_id")) == target_warehouse}

    top_fields = summarize_fields(products)
    sku_fields = summarize_fields(products, "skus")
    literal_material_keys = sorted({key for key in set(top_fields) | set(sku_fields)
        if any(marker in key.lower() for marker in ("material", "texture", "材质", "caizhi"))})
    relevant_names = ("standard", "type", "model", "material", "texture", "area", "comment",
                      "fullname", "usercode", "xcode", "isstop", "isclass")
    relevant_top = {key: value for key, value in top_fields.items()
                    if any(name in key.lower() for name in relevant_names)}
    relevant_sku = {key: value for key, value in sku_fields.items()
                    if any(name in key.lower() for name in relevant_names)}

    sample_codes = {"LS3073", "LS0123"}
    samples = []
    for product in products:
        codes = {text(product.get("usercode")).upper()}
        codes.update(text(row.get("usercode") or row.get("xcode")).upper()
                     for row in product.get("skus") or [])
        if codes & sample_codes:
            samples.append({
                "matched_codes": sorted(codes & sample_codes),
                "product": {key: value for key, value in product.items()
                            if key != "skus" and present(value)},
                "skus": product.get("skus") or [],
            })

    report = {
        "target_warehouse_id": target_warehouse,
        "product_records": len(products),
        "class_records": sum(bool(row.get("isclass")) for row in products),
        "stopped_records": sum(bool(row.get("isstop")) for row in products),
        "products_without_variants": products_without_variants,
        "catalog_variant_identities": len(catalog),
        "catalog_printable_codes": sum(bool(row["sku"]) for row in catalog.values()),
        "invalid_variants": invalid_variants,
        "published_stock_identities_all_warehouses": len(all_stock_keys),
        "published_stock_identities_target_warehouse": len(target_stock_keys),
        "catalog_missing_from_target_stock": len(set(catalog) - target_stock_keys),
        "catalog_missing_from_all_stock": len(set(catalog) - all_stock_keys),
        "literal_material_keys": literal_material_keys,
        "relevant_product_fields": relevant_top,
        "relevant_sku_fields": relevant_sku,
        "sample_products": samples,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
