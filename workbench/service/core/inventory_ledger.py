"""Durable 08390 inventory movements, balance history and subset refreshes."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .api_source import SourceIncomplete
from .source_retry import stable_page
from .erp_api import ErpUnavailable
from .logistics import inventory_supply
from .store import dumps, timestamp


BUSINESS_WAREHOUSE_ID = "29871293290508390"
HISTORY_DAYS = 180
MOVEMENT_TYPES = (34, 6, 143, 11, 45, 142, 21, 9, 14, 16, 139, 126, 152, 153)
# Verified against this account's signed qty and inout values on 2026-09-29.
# Empty-sample types remain readable, but cannot make a balance trustworthy.
VERIFIED_TYPES = frozenset({34, 6, 11, 45, 9, 14, 16})
TYPE_NAMES = {
    34: "进货入库单", 6: "进货退货单", 143: "进货换货单",
    11: "销售出库单", 45: "销售退货单", 142: "销售换货单",
    21: "调拨单", 9: "报损单", 14: "报溢单", 16: "生产组装单",
    139: "其他出库单", 126: "其他入库单", 152: "内部领用单",
    153: "领用退回单",
}
TYPE_CODES_BY_NAME = {name: code for code, name in TYPE_NAMES.items()}
PAGE_SIZE = 500


def _text(value):
    return str(value).strip() if value is not None else ""


def decimal_text(value):
    if isinstance(value, bool):
        raise ValueError("数量不能是布尔值")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError("数量格式无效") from None
    if not number.is_finite():
        raise ValueError("数量必须是有限数")
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _iso_datetime(value, default=""):
    raw = _text(value)
    if not raw:
        return default
    candidate = raw.replace("/", "-").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.isoformat(timespec="seconds")


def _day(value):
    result = _iso_datetime(value)
    return result[:10] if result else ""


def _items(value, name):
    if isinstance(value, dict):
        value = value.get(name)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SourceIncomplete(f"ERP {name} 集合结构不完整")
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(profile, bill_id, line_id, warehouse_id):
    raw = f"inventory-movement-v1\0{profile}\0{bill_id}\0{line_id}\0{warehouse_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _inventory_key(row):
    return tuple(_text(row.get(name)) for name in
                 ("profile_id", "warehouse_id", "product_id", "sku_id", "base_unit"))


class InventoryLedger:
    def __init__(self, store, warehouse_id=BUSINESS_WAREHOUSE_ID):
        self.store = store
        self.warehouse_id = _text(warehouse_id)

    def _window(self, profile, now, force_full):
        with self.store.connect() as database:
            row = database.execute(
                """SELECT * FROM inventory_sync_watermarks
                   WHERE profile_id=? AND warehouse_id=? AND source='billcenter'""",
                (str(profile), self.warehouse_id),
            ).fetchone()
        target_start = (now - timedelta(days=HISTORY_DAYS)).replace(
            hour=0,minute=0,second=0,microsecond=0)
        full = bool(force_full or row is None or not row["last_full_at"] or
                    not row['coverage_from'] or
                    datetime.fromisoformat(row['coverage_from']) > target_start)
        if full:
            begin = target_start
            coverage_from = begin.isoformat(timespec="seconds")
        else:
            try:
                last = datetime.fromisoformat(row["last_success_at"])
                if last.tzinfo is None:
                    raise ValueError()
            except (TypeError, ValueError):
                raise SourceIncomplete("库存台账同步水位无效") from None
            begin = max(last - timedelta(hours=72), target_start)
            coverage_from = row["coverage_from"] or begin.isoformat(timespec="seconds")
        return {
            "full_reconcile": full,
            "begin": begin.strftime("%Y-%m-%d %H:%M:%S"),
            "end": now.strftime("%Y-%m-%d %H:%M:%S"),
            "coverage_from": coverage_from,
            "coverage_to": now.isoformat(timespec="seconds"),
            "previous_last_full_at": row["last_full_at"] if row else None,
        }

    def _fetch_bills(self, client, window, progress):
        params = {
            "overbegin": window["begin"], "overend": window["end"],
            "vchtype": list(MOVEMENT_TYPES), "showredbill": True,
            "hascustom": True, "pagesize": PAGE_SIZE,
        }
        output, seen, first = [], set(), None
        for page in range(1, 2001):
            response = client.call("erp.bill.billcenterlist", dict(params, pageindex=page))
            bills = _items(response.get("details") if isinstance(response, dict) else None, "details")
            if page == 1:
                first = stable_page(bills)
            for bill in bills:
                identity = _text(bill.get("vchcode"))
                if not identity or identity in seen:
                    raise SourceIncomplete("库存台账单据缺少身份或分页重复")
                seen.add(identity)
                output.append(bill)
            progress({"source": "inventory_ledger", "current": len(output), "total": 0})
            if len(bills) < PAGE_SIZE:
                if page > 1:
                    check = client.call("erp.bill.billcenterlist", dict(params, pageindex=1))
                    if stable_page(_items(check.get("details"), "details")) != first:
                        raise SourceIncomplete("库存台账分页期间发生变化")
                return output
        raise SourceIncomplete("库存台账超过分页上限")

    def _normalize(self, profile, bills, catalog_keys, fetched_at):
        events, issues, seen_lines = [], [], set()
        for bill in bills:
            bill_id = _text(bill.get("vchcode"))
            code_text = _text(bill.get("vchtype"))
            try:
                type_code = int(code_text)
            except ValueError:
                type_code = TYPE_CODES_BY_NAME.get(_text(bill.get("vchtypename")), 0)
            document_type = _text(bill.get("vchtypename")) or TYPE_NAMES.get(type_code, code_text)
            posted_at = _iso_datetime(bill.get("overtime"), _iso_datetime(bill.get("date")))
            business_date = _day(bill.get("date")) or posted_at[:10]
            if type_code not in MOVEMENT_TYPES or not bill_id or not posted_at or not business_date:
                issues.append({"code": "movement_header_incomplete",
                               "reason": "库存变动单据缺少类型、身份或时间"})
                continue
            try:
                details = _items(bill.get("details"), "details")
            except SourceIncomplete:
                issues.append({"code": "movement_details_incomplete",
                               "reason": f"{document_type}{_text(bill.get('number'))}明细结构不完整"})
                continue
            for line in details:
                line_id = _text(line.get("dlyorder"))
                line_key = (bill_id, line_id)
                product_id = _text(line.get("ptypeid"))
                sku_id = _text(line.get("skuid"))
                line_warehouse = _text(line.get("ktypeid") or bill.get("ktypeid"))
                first_warehouse = _text(bill.get("ktypeid"))
                second_warehouse = _text(line.get("ktypeid2") or bill.get("ktypeid2"))
                relevant = line_warehouse == self.warehouse_id
                transfer_side = ""
                if type_code == 21 and not relevant:
                    if first_warehouse == self.warehouse_id and second_warehouse != self.warehouse_id:
                        relevant, transfer_side = True, "out"
                    elif second_warehouse == self.warehouse_id and first_warehouse != self.warehouse_id:
                        relevant, transfer_side = True, "in"
                if not relevant:
                    continue
                inventory_key = (str(profile), self.warehouse_id, product_id, sku_id, "1")
                issue_scope = {"scope": "inventory", "inventory_key": list(inventory_key)}
                if not line_id or line_key in seen_lines:
                    issues.append({**issue_scope, "code": "movement_line_identity_invalid",
                                   "reason": "库存变动明细缺少唯一身份"})
                    continue
                seen_lines.add(line_key)
                if inventory_key not in catalog_keys:
                    issues.append({**issue_scope, "code": "movement_product_unmatched",
                                   "reason": "库存变动商品无法关联当前08390目录"})
                    continue
                try:
                    raw_quantity = decimal_text(line.get("qty"))
                except ValueError:
                    issues.append({**issue_scope, "code": "movement_quantity_invalid",
                                   "reason": "库存变动数量无效"})
                    continue
                signed = Decimal(raw_quantity)
                if transfer_side:
                    signed = abs(signed) if transfer_side == "in" else -abs(signed)
                inout_raw = line.get("inout")
                try:
                    inout = int(inout_raw)
                except (TypeError, ValueError):
                    inout = None
                sign_matches = (inout == 0 and signed >= 0) or (inout == 1 and signed <= 0)
                verified = bool(type_code in VERIFIED_TYPES and not transfer_side and sign_matches)
                if not verified:
                    issues.append({**issue_scope, "code": "movement_semantics_unverified",
                        "reason": f"{document_type}的数量方向尚无该账套真实样本验收"})
                signed_text = decimal_text(signed)
                event = {
                    "event_id": _event_id(str(profile), bill_id, line_id, self.warehouse_id),
                    "profile_id": str(profile), "warehouse_id": self.warehouse_id,
                    "product_id": product_id, "sku_id": sku_id, "base_unit": "1",
                    "source_bill_id": bill_id, "source_line_id": line_id,
                    "document_no": _text(bill.get("number")),
                    "document_type_code": str(type_code), "document_type": document_type,
                    "business_date": business_date, "posted_at": posted_at,
                    "party": _text(bill.get("bfullname")),
                    "warehouse_name": _text(line.get("kfullname") or bill.get("kfullname")),
                    "raw_quantity": raw_quantity, "signed_quantity": signed_text,
                    "inout": inout, "direction": "in" if signed > 0 else "out" if signed < 0 else "neutral",
                    "redold": bool(bill.get("redold")), "redword": bool(bill.get("redword")),
                    "summary": _text(bill.get("summary")), "comment": _text(bill.get("comment")),
                    "semantics_verified": verified, "updated_at": fetched_at,
                }
                event["source_hash"] = hashlib.sha256(_canonical(event).encode()).hexdigest()
                events.append(event)
        events.sort(key=lambda item: (item["posted_at"], item["source_bill_id"],
                                      item["source_line_id"], item["event_id"]))
        unique_issues = {_canonical(item): item for item in issues}
        return events, list(unique_issues.values())

    def prepare(self, client, profile, inventory_rows, started, force_full=False, progress=None):
        """Read a complete window but defer every SQLite mutation to publish()."""
        progress = progress or (lambda value: None)
        window = self._window(str(profile), started, force_full)
        snapshots = []
        catalog_keys = set()
        for row in inventory_rows:
            key = _inventory_key(row)
            if key[0] != str(profile) or key[1] != self.warehouse_id or not key[2] or not key[4]:
                raise SourceIncomplete("库存台账快照身份不完整")
            if key in catalog_keys:
                raise SourceIncomplete("库存台账快照身份重复")
            catalog_keys.add(key)
            snapshots.append({
                "profile_id": key[0], "warehouse_id": key[1], "product_id": key[2],
                "sku_id": key[3], "base_unit": key[4],
                "physical_quantity": decimal_text(row.get("stock_quantity", row.get("physical_qty", 0))),
                "saleable_quantity": decimal_text(row.get("available_stock", row.get("saleable_qty", 0))),
            })
        try:
            bills = self._fetch_bills(client, window, progress)
            events, issues = self._normalize(str(profile), bills, catalog_keys,
                                             started.isoformat(timespec="seconds"))
            token = uuid.uuid4().hex
            for event in events:
                event["sync_token"] = token
            return {
                "commit": True, "profile_id": str(profile), "warehouse_id": self.warehouse_id,
                "window": window, "sync_token": token, "events": events,
                "snapshots": snapshots, "issues": issues,
                "bill_count": len(bills), "fetched_at": started.isoformat(timespec="seconds"),
            }
        except (ErpUnavailable, SourceIncomplete) as error:
            return {
                "commit": False, "profile_id": str(profile), "warehouse_id": self.warehouse_id,
                "window": window, "events": [], "snapshots": snapshots,
                "issues": [{"code": "movement_sync_unavailable", "reason": str(error)}],
                "bill_count": 0, "fetched_at": started.isoformat(timespec="seconds"),
            }

    @staticmethod
    def validate_stage(stage, profile):
        if not isinstance(stage, dict) or str(stage.get("profile_id")) != str(profile):
            raise SourceIncomplete("库存台账批次账套无效")
        if stage.get("warehouse_id") != BUSINESS_WAREHOUSE_ID:
            raise SourceIncomplete("库存台账仅允许08390仓库")
        for name in ("events", "snapshots", "issues"):
            if not isinstance(stage.get(name), list) or any(not isinstance(x, dict) for x in stage[name]):
                raise SourceIncomplete("库存台账批次结构无效")
        return stage

    @staticmethod
    def persist(database, stage, data_version):
        if not stage or stage.get("commit") is not True:
            return
        profile, warehouse = str(stage["profile_id"]), str(stage["warehouse_id"])
        window = stage["window"]
        begin = _iso_datetime(window["begin"])
        end = _iso_datetime(window["end"])
        if not begin or not end or end < begin:
            raise SourceIncomplete("库存台账窗口无效")
        # A successful page set authoritatively replaces its overlap, including
        # bills that were edited or removed since the previous read.
        database.execute(
            """DELETE FROM inventory_movements WHERE profile_id=? AND warehouse_id=?
               AND posted_at>=? AND posted_at<=?""", (profile, warehouse, begin, end),
        )
        for event in stage["events"]:
            database.execute(
                """INSERT INTO inventory_movements VALUES(
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event["event_id"], profile, warehouse, event["product_id"], event["sku_id"],
                 event["base_unit"], event["source_bill_id"], event["source_line_id"],
                 event["document_no"], event["document_type_code"], event["document_type"],
                 event["business_date"], event["posted_at"], event["party"],
                 event["warehouse_name"], event["raw_quantity"], event["signed_quantity"],
                 event["inout"], event["direction"], int(event["redold"]), int(event["redword"]),
                 event["summary"], event["comment"], event["source_hash"],
                 int(event["semantics_verified"]), event["sync_token"], event["updated_at"]),
            )
        global_issue = any(item.get("scope") != "inventory" for item in stage["issues"])
        affected = {tuple(map(str, item.get("inventory_key", []))) for item in stage["issues"]
                    if item.get("scope") == "inventory"}
        reasons = {_canonical(item): _text(item.get("reason")) for item in stage["issues"]}
        now = stage["fetched_at"]
        for snapshot in stage["snapshots"]:
            key = _inventory_key(snapshot)
            unverified = database.execute(
                """SELECT 1 FROM inventory_movements WHERE profile_id=? AND warehouse_id=?
                   AND product_id=? AND sku_id=? AND base_unit=? AND semantics_verified=0 LIMIT 1""",
                key,
            ).fetchone()
            incomplete = global_issue or key in affected or bool(unverified)
            relevant_reasons = [value for item, value in reasons.items()
                                if json.loads(item).get("scope") != "inventory" or
                                tuple(map(str, json.loads(item).get("inventory_key", []))) == key]
            reason = "；".join(dict.fromkeys(value for value in relevant_reasons if value))[:1000]
            database.execute(
                """INSERT OR REPLACE INTO inventory_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*key, snapshot["physical_quantity"], snapshot["saleable_quantity"], now,
                 data_version, "incomplete" if incomplete else "reconciled",
                 window["coverage_from"], window["coverage_to"], reason),
            )
        last_full = now if window["full_reconcile"] else window.get("previous_last_full_at")
        payload = {"bill_count": stage["bill_count"], "event_count": len(stage["events"]),
                   "issue_count": len(stage["issues"]), "full_reconcile": bool(window["full_reconcile"])}
        database.execute(
            """INSERT OR REPLACE INTO inventory_sync_watermarks VALUES(
               ?,?,'billcenter',?,?,?,?,?,?,?)""",
            (profile, warehouse, now, window["coverage_from"], window["coverage_to"],
             last_full, "incomplete" if stage["issues"] else "succeeded",
             dumps(payload), timestamp()),
        )

    def history(self, record_id, inbound_target=4, limit=120, days=None, offset=0):
        if not 1 <= int(inbound_target) <= 20 or not 1 <= int(limit) <= 120 or \
                int(offset)<0 or int(offset)>10000 or (days is not None and int(days) not in {30,90,180}):
            raise ValueError("库存历史范围无效")
        window_from = (datetime.now(ZoneInfo('Asia/Shanghai'))-timedelta(days=int(days)))\
            .isoformat(timespec='seconds') if days is not None else None
        with self.store.connect() as database:
            record = database.execute(
                "SELECT payload FROM records WHERE kind='inventory' AND id=?", (str(record_id),),
            ).fetchone()
            if record is None:
                raise KeyError("商品库存记录不存在")
            product = json.loads(record["payload"])
            key = _inventory_key(product)
            if key[1] != self.warehouse_id:
                raise KeyError("商品不属于08390仓库")
            snapshot = database.execute(
                """SELECT * FROM inventory_snapshots WHERE profile_id=? AND warehouse_id=?
                   AND product_id=? AND sku_id=? AND base_unit=?""", key,
            ).fetchone()
            rows = database.execute(
                """SELECT * FROM inventory_movements WHERE profile_id=? AND warehouse_id=?
                   AND product_id=? AND sku_id=? AND base_unit=?
                   AND (? IS NULL OR posted_at>=?)
                   ORDER BY posted_at DESC,source_bill_id DESC,source_line_id DESC LIMIT ?""",
                (*key,window_from,window_from,int(offset)+int(limit)+1),
            ).fetchall()
            total = database.execute(
                """SELECT count(*) FROM inventory_movements WHERE profile_id=? AND warehouse_id=?
                   AND product_id=? AND sku_id=? AND base_unit=? AND posted_at>=?""",
                (*key,window_from),
            ).fetchone()[0] if window_from is not None else None
        selected, positives = [], 0
        for row in rows:
            if len(selected) >= int(limit)+int(offset):
                break
            selected.append(row)
            if Decimal(row["signed_quantity"]) > 0:
                positives += 1
                if days is None and positives >= int(inbound_target):
                    break
        truncated = len(rows) > len(selected)
        status = snapshot["reconciliation_status"] if snapshot else "pending"
        balances_allowed = bool(snapshot and status == "reconciled" and
                                all(row["semantics_verified"] for row in selected))
        balance = Decimal(snapshot["physical_quantity"]) if snapshot else Decimal(0)
        movements = []
        for row in selected:
            after = decimal_text(balance) if balances_allowed else None
            signed = Decimal(row["signed_quantity"])
            event = {
                "id": row["event_id"], "posted_at": row["posted_at"],
                "date": row["business_date"], "document_no": row["document_no"],
                "document_type": row["document_type"], "party": row["party"],
                "quantity": row["signed_quantity"], "direction": row["direction"],
                "balance_after": after,
                "reconciliation_status": "reconciled" if balances_allowed and row["semantics_verified"] else "incomplete",
            }
            if len(movements) >= int(offset):
                movements.append(event)
            else:
                movements.append(None)
            if balances_allowed:
                balance -= signed
        movements = [event for event in movements[int(offset):] if event is not None]
        current = {
            "physical_quantity": snapshot["physical_quantity"] if snapshot else _text(product.get("stock_quantity")),
            "saleable_quantity": snapshot["saleable_quantity"] if snapshot else _text(product.get("available_stock")),
            "as_of": snapshot["as_of"] if snapshot else "",
            "data_version": snapshot["data_version"] if snapshot else "",
        }
        return {
            "record_id": str(record_id),
            "product": {name: product.get(name, "") for name in
                        ("profile_id", "warehouse_id", "product_id", "sku_id", "base_unit",
                         "sku", "product_name", "unit_name")},
            "snapshot": current, "status": status,
            "coverage": {
                "from": snapshot["coverage_from"] if snapshot else "",
                "to": snapshot["coverage_to"] if snapshot else "",
                "positive_inbounds": positives, "returned": len(movements),
                "limit": int(limit), "total": total, "truncated": truncated,
                "days": int(days) if days is not None else None,
                "next_offset": int(offset)+len(movements) if truncated else None,
                "reason": snapshot["reason"] if snapshot else "库存台账尚未同步",
            },
            "movements": movements, "pending_adjustments": [],
        }


class InventorySubsetRefresher:
    """Refresh selected products atomically after a LAN adjustment draft."""
    def __init__(self, store, client, warehouse_id=BUSINESS_WAREHOUSE_ID):
        self.store, self.client, self.warehouse_id = store, client, str(warehouse_id)

    @staticmethod
    def _request_product_ids(product_ids=None, identities=None):
        values = set()
        for value in product_ids or []:
            item = _text(value)
            if not item or not item.isdigit():
                raise ValueError("商品 ID 无效")
            values.add(item)
        for identity in identities or []:
            if not isinstance(identity, dict):
                raise ValueError("库存身份无效")
            if _text(identity.get("warehouse_id")) != BUSINESS_WAREHOUSE_ID:
                raise ValueError("库存子集刷新仅允许08390仓库")
            item = _text(identity.get("product_id"))
            if not item or not item.isdigit():
                raise ValueError("库存身份缺少商品 ID")
            values.add(item)
        if not values or len(values) > 100:
            raise ValueError("库存子集刷新每次需 1 至 100 个商品")
        return sorted(values, key=int)

    def _read_rows(self, product_ids, progress):
        profile = self.client.account()
        output = []
        for offset in range(0, len(product_ids), 500):
            selected = product_ids[offset:offset + 500]
            expected, seen = None, set()
            for page in range(1, 2001):
                result = self.client.call("erp.goodsstocks.list", {
                    "ptypeids": [int(value) for value in selected],
                    "ktypeids": [int(self.warehouse_id)],
                    "pageno": page, "pagesize": 500,
                })
                items = _items(result.get("goodsstocks") if isinstance(result, dict) else None,
                               "goodsstocks")
                total = result.get("totalresults") if isinstance(result, dict) else None
                if isinstance(total, bool) or not str(total).isdigit():
                    raise SourceIncomplete("库存子集刷新缺少分页总数")
                if expected is not None and expected != int(total):
                    raise SourceIncomplete("库存子集刷新期间数量变化")
                expected = int(total)
                for row in items:
                    identity = tuple(_text(row.get(name)) for name in
                                     ("profileid", "ktypeid", "ptypeid", "skuid"))
                    if (identity in seen or identity[0] != str(profile) or
                            identity[1] != self.warehouse_id or identity[2] not in selected):
                        raise SourceIncomplete("库存子集返回范围外或重复数据")
                    seen.add(identity)
                    output.append(row)
                progress({"source": "inventory_subset", "current": offset + len(selected),
                          "total": len(product_ids)})
                if len(seen) == expected:
                    break
                if len(seen) > expected or len(items) != 500:
                    raise SourceIncomplete("库存子集分页未闭合")
            else:
                raise SourceIncomplete("库存子集超过分页上限")
        return str(profile), output

    def refresh(self, product_ids=None, identities=None, progress=None):
        progress = progress or (lambda value: None)
        requested = self._request_product_ids(product_ids, identities)
        with self.store.connect() as database:
            version_row = database.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
            before_version = json.loads(version_row[0]) if version_row else ""
            rows = [json.loads(row["payload"]) for row in database.execute(
                "SELECT payload FROM records WHERE kind='inventory' ORDER BY id")]
            order_rows = [json.loads(row["payload"]) for row in database.execute(
                "SELECT payload FROM records WHERE kind='orders' ORDER BY id")]
        selected_rows = [row for row in rows if _text(row.get("product_id")) in requested]
        if not selected_rows or {row["product_id"] for row in selected_rows} != set(requested):
            raise SourceIncomplete("库存子集中有商品不在已发布目录")
        if any(_text(row.get("warehouse_id")) != self.warehouse_id for row in selected_rows):
            raise SourceIncomplete("库存子集混入其他仓库")
        profile, fresh = self._read_rows(requested, progress)
        if any(_text(row.get("profile_id")) != profile for row in selected_rows):
            raise SourceIncomplete("库存子集账套与 ERP 授权不一致")
        stock = {}
        for row in fresh:
            key = (_text(row.get("productid")) or _text(row.get("ptypeid")), _text(row.get("skuid")))
            if key in stock:
                raise SourceIncomplete("库存子集包含重复商品/SKU")
            stock[key] = row
        expected_keys = {(_text(row.get("product_id")), _text(row.get("sku_id"))) for row in selected_rows}
        if set(stock) - expected_keys:
            raise SourceIncomplete("ERP 返回了未发布的库存 SKU")
        now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        demand_by_identity = {}
        for order_row in order_rows:
            identity = _inventory_key(order_row)
            if all(identity):
                quantity = Decimal(decimal_text(order_row.get("quantity")))
                demand_by_identity[identity] = demand_by_identity.get(identity, Decimal(0)) + quantity
        updated = []
        for row in selected_rows:
            found = stock.get((_text(row.get("product_id")), _text(row.get("sku_id"))))
            physical = decimal_text(found.get("qty")) if found else "0"
            saleable = decimal_text(found.get("saleqty")) if found else "0"
            total_demand = demand_by_identity.get(_inventory_key(row), Decimal(0))
            balance = inventory_supply({
                "physical_qty": physical,
                "saleable_qty": saleable,
            }, total_demand, allow_negative_physical=True)
            row.update(
                stock_quantity=physical,
                available_stock=saleable,
                purchase_pending=decimal_text(balance["pending"]),
                inferred_other_reserved=decimal_text(balance["inferred_other_reserved"]),
                effective_physical=decimal_text(balance["effective_physical"]),
            )
            updated.append(row)
        updated_by_id = {row["id"]: row for row in updated}
        with self.store.connect(write=True) as database:
            current = database.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
            current_version = json.loads(current[0]) if current else ""
            if current_version != before_version:
                raise SourceIncomplete("完整数据版本已更新，库存子集未发布")
            for identity, payload in updated_by_id.items():
                current_row = database.execute(
                    "SELECT payload FROM records WHERE kind='inventory' AND id=?", (identity,),
                ).fetchone()
                if current_row is None:
                    raise SourceIncomplete("库存子集发布前商品已变更")
                old = json.loads(current_row["payload"])
                if _inventory_key(old) != _inventory_key(payload):
                    raise SourceIncomplete("库存子集发布前身份已变更")
                database.execute("UPDATE records SET payload=? WHERE kind='inventory' AND id=?",
                                 (dumps(payload), identity))
            all_records = {}
            for kind in ("orders", "inventory", "purchases"):
                all_records[kind] = [json.loads(row[0]) for row in database.execute(
                    "SELECT payload FROM records WHERE kind=? ORDER BY id", (kind,))]
            new_version = hashlib.sha256(json.dumps(
                all_records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()[:20]
            database.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)",
                             (dumps(new_version),))
            database.execute("INSERT OR REPLACE INTO settings VALUES ('data_published_at',?)",
                             (dumps(now),))
            source = database.execute("SELECT payload FROM sources WHERE kind='inventory'").fetchone()
            if source:
                metadata = json.loads(source[0])
                metadata.update(source_modified_at=now, imported_at=now, sha256=new_version,
                                subset_refresh={"product_count": len(requested)})
                database.execute("UPDATE sources SET payload=? WHERE kind='inventory'", (dumps(metadata),))
            for payload in updated:
                key = _inventory_key(payload)
                prior = database.execute(
                    """SELECT physical_quantity,reconciliation_status,coverage_from,coverage_to,reason
                       FROM inventory_snapshots WHERE profile_id=? AND warehouse_id=?
                       AND product_id=? AND sku_id=? AND base_unit=?""", key,
                ).fetchone()
                physical_changed = bool(prior and prior["physical_quantity"] != payload["stock_quantity"])
                status = "incomplete" if physical_changed else prior["reconciliation_status"] if prior else "pending"
                reason = "实物库存已变化，等待单据流水同步核对" if physical_changed else prior["reason"] if prior else "等待库存台账同步"
                database.execute(
                    """INSERT OR REPLACE INTO inventory_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*key, payload["stock_quantity"], payload["available_stock"], now,
                     new_version, status, prior["coverage_from"] if prior else "",
                     prior["coverage_to"] if prior else "", reason),
                )
        return {"data_version": new_version, "source_updated_at": now,
                "updated": len(updated), "product_ids": requested}
