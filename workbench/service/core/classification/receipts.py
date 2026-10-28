"""Production rules extracted from NAS 20260924 baseline; no I/O or ERP writes."""
from __future__ import annotations
import copy
import json
import datetime as dt
from collections import defaultdict
from . import allocation
WARNING_DAYS = 7


def observed_time(value):
    """ERP wall times are China-local; never compare timestamps as strings."""
    if not value or len(str(value).strip()) <= 10:
        return None
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return result


def observation_day(event):
    """The ERP history window is selected by posting time, not bill date.

    Keep the original business date and event ID for allocation/history. Only
    coverage checks use this day. Legacy ledgers without posting evidence
    retain their date-based behavior; invalid evidence must not fall back.
    """
    if event.get("posted_at"):
        posted = observed_time(event["posted_at"])
        if posted is None:
            raise ValueError("入库事件过账时间无效")
        return posted.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    return dt.date.fromisoformat(str(event.get("posted_date") or event["date"])).isoformat()


def receipt_lag_proven(prior, lot):
    """A later poll may reveal the receipt already present in a ready stock read.

    Only the exact uncovered observation window is proof. Legacy ledgers and
    genuinely later replenishment keep the old ready-inventory protection.
    """
    if not prior:
        return False
    ready = observed_time(prior.get("inventory_ready_observed_at"))
    covered = observed_time(prior.get("inventory_ready_receipt_covered_through"))
    posted = observed_time(lot.get("posted_at"))
    return bool(ready and covered and posted and covered < posted <= ready)


def group_key(event):
    return event["warehouse"], event["sku"]


def reconcile(events: list[dict], state: dict | None, period: tuple[str, str], stock: dict):
    if state is not None and state.get("version") != 1:
        raise ValueError("入库批次状态版本异常，不能清空后重新计数")
    result = copy.deepcopy(state) if state is not None else {"version": 1, "events": {}, "balances": {}}
    previous, balances = result["events"], result["balances"]
    if result.get("period_end", "") > period[1]:
        raise ValueError("不能用较旧的进货明细回退入库批次状态")
    fresh = {e["id"]: e for e in events}
    changes = []
    received_increases = {}
    source_reductions, return_reductions = defaultdict(float), defaultdict(float)
    for identity, old in previous.items():
        if period[0] <= observation_day(old) <= period[1] and identity not in fresh:
            fresh[identity] = dict(old, quantity=0.0)
    for identity, event in fresh.items():
        old_quantity = previous.get(identity, {}).get("quantity", 0.0)
        delta = event["quantity"] - old_quantity
        if abs(delta) > allocation.EPSILON:
            changes.append((event, delta))

    def consume(key, quantity, target=None):
        if quantity <= allocation.EPSILON:
            return
        candidates = sorted((e for e in previous.values() if group_key(e) == key),
                            key=lambda e: (e["id"] != target, e["date"], e["document"], e["id"]))
        for event in candidates:
            identity = event["id"]
            taken = min(max(0, balances.get(identity, 0.0)), quantity)
            balances[identity] = max(0, balances.get(identity, 0.0) - taken)
            quantity -= taken
            if quantity <= allocation.EPSILON:
                break

    # Corrections within the rolling window replace earlier quantities; unchanged
    # exports never replenish quantities already consumed on a previous update.
    for event, delta in sorted(changes, key=lambda item: (item[0]["date"], item[1] < 0, item[0]["id"])):
        identity = event["id"]
        old_quantity = previous.get(identity, {}).get("quantity", 0.0)
        previous[identity] = dict(event)
        if event["type"] in {"进货入库单", "内部组装／拆卸"}:
            received_delta = max(0, event["quantity"]) - max(0, old_quantity)
            if received_delta > 0:
                balances[identity] = balances.get(identity, 0.0) + received_delta
                received_increases[identity] = received_delta
            elif received_delta < 0:
                source_reductions[identity] += -received_delta
                consume(group_key(event), -received_delta, identity)
            # A red inbound may be a separate negative-quantity document rather
            # than a correction of its original positive event. Split positive
            # and negative components so crossing +q to -q consumes exactly
            # the signed reduction, never both that delta and its components.
            # Red reversals do not resurrect already consumed batch provenance.
            red_increase = max(0, -event["quantity"]) - max(0, -old_quantity)
            if red_increase > allocation.EPSILON:
                return_reductions[group_key(event)] += red_increase
                consume(group_key(event), red_increase)
        elif delta < 0:
            return_reductions[group_key(event)] += -delta
            consume(group_key(event), -delta)
    previous.update(copy.deepcopy(fresh))
    totals = defaultdict(float)
    for identity, quantity in balances.items():
        if identity not in previous or quantity < -allocation.EPSILON:
            raise ValueError("入库批次数量状态损坏")
        totals[group_key(previous[identity])] += quantity
    covered_warehouses = {key[0] for key in stock}
    limits = dict(stock)
    for key in totals:
        if key[0] in covered_warehouses:
            limits.setdefault(key, 0)
    for key, limit in limits.items():
        consume(key, max(0, totals[key] - max(0, limit)))
    result["period_start"], result["period_end"] = period
    # Transient observation, consumed by classify before the ledger is committed.
    result["_received_increases"] = received_increases
    result["_source_reductions"] = dict(source_reductions)
    result["_return_reductions"] = dict(return_reductions)
    return result


def order_key(demand):
    return json.dumps([demand.order, demand.warehouse, demand.sku, demand.date.isoformat()], ensure_ascii=False)


def previous_orders(state):
    version = state.get("dispatch_version")
    if version is None:
        return {}
    if version != 1 or not isinstance(state.get("orders"), dict):
        raise ValueError("订单分配状态损坏，不能静默重建包装分工")
    for key, item in state["orders"].items():
        identity = json.loads(key)
        if len(identity) != 4 or item.get("route") not in {None, "inventory", "arrival"}:
            raise ValueError("订单分配状态标识或分工异常")
        quantity = allocation.number(item["quantity"], "历史订单需求")
        actual = allocation.number(item["actual"], "历史订单现货")
        if (quantity <= 0 or not 0 <= actual <= quantity + allocation.EPSILON
                or ((actual + allocation.EPSILON >= quantity) != (item['route'] is not None))):
            raise ValueError("订单分配状态数量异常")
        total = 0.0
        found = set()
        for part in item["receipts"]:
            event = state["events"].get(part["id"])
            amount = allocation.number(part["quantity"], "历史来货分配")
            if (event is None or group_key(event) != tuple(identity[1:3]) or amount <= 0
                    or (part["id"], part.get("arrival")) in found or not isinstance(part.get("arrival"), bool)
                    or (part["arrival"] and event["date"] < identity[3])):
                raise ValueError("订单来货分配记录异常")
            found.add((part["id"], part["arrival"]))
            total += amount
        if total > actual + allocation.EPSILON:
            raise ValueError("订单来货分配超过历史现货数量")
        claim_total, claim_ids = 0.0, set()
        for claim in item.get("arrival_claims", []):
            event = state["events"].get(claim["id"])
            amount = allocation.number(claim["quantity"], "历史来货来源数量")
            if (event is None or group_key(event) != tuple(identity[1:3]) or amount <= 0
                    or claim["id"] in claim_ids or event["date"] < identity[3]):
                raise ValueError("历史来货来源记录异常")
            claim_ids.add(claim["id"])
            claim_total += amount
        if claim_total > quantity + allocation.EPSILON:
            raise ValueError("历史来货来源超过订单需求")
    return state["orders"]


def classify(rows: list[dict], state: dict, today: dt.date):
    previous = previous_orders(state)
    increases = state.pop("_received_increases", {})
    source_reductions = state.pop("_source_reductions", {})
    return_reductions = state.pop("_return_reductions", {})
    historical_dates = defaultdict(set)
    for encoded in previous:
        identity = json.loads(encoded)
        historical_dates[tuple(identity[:3])].add(identity[3])
    for row in rows:
        current = json.loads(order_key(row["demand"]))
        old_dates = historical_dates.get(tuple(current[:3]))
        if old_dates and current[3] not in old_dates:
            raise ValueError("历史订单日期变化，来货归属待核对")
    queues = defaultdict(list)
    lots_by_id = {}
    for identity, quantity in state["balances"].items():
        if quantity > allocation.EPSILON:
            event = state["events"][identity]
            lot = dict(event, remaining=quantity, fresh=min(quantity, increases.get(identity, 0)))
            queues[group_key(event)].append(lot)
            lots_by_id[identity] = lot
    for lots in queues.values():
        lots.sort(key=lambda e: (e["date"], e["document"], e["id"]))
    unknown = {row["demand"].key: max(0, row["stock"] - sum(e["remaining"] for e in queues[row["demand"].key]))
               for row in rows}
    output = copy.deepcopy(rows)
    ordered = sorted(output, key=lambda r: (r["demand"].warehouse, r["demand"].sku, r["demand"].date, r["demand"].order))
    parts = {order_key(row["demand"]): {} for row in ordered}
    new_allocated = defaultdict(float)

    def take(row, lot, wanted, arrival):
        amount = min(wanted, lot["remaining"])
        if amount <= allocation.EPSILON:
            return 0.0
        identity = order_key(row["demand"])
        fresh = max(0, amount - (lot["remaining"] - lot["fresh"]))
        lot["remaining"] -= amount
        lot["fresh"] -= fresh
        part = parts[identity].setdefault((lot["id"], arrival), {"id": lot["id"], "quantity": 0.0, "arrival": arrival})
        part["quantity"] += amount
        if arrival:
            new_allocated[identity] += fresh
        return amount

    # Retain receipt provenance within each order's freshly computed FIFO quota.
    # Overflow elsewhere must not transfer an unshipped receipt to another order.
    for row in ordered:
        identity = order_key(row["demand"])
        need = row["actual"]
        for part in previous.get(identity, {}).get("receipts", []):
            lot = lots_by_id.get(part["id"])
            if lot is not None:
                arrival = part["arrival"] or (
                    lot["date"] >= row["demand"].date.isoformat()
                    and receipt_lag_proven(previous.get(identity), lot))
                need -= take(row, lot, min(need, part["quantity"]), arrival)

    # Recover dormant claims only after every order has retained its current
    # receipt allocation. Never reclaim stock already held by another order.
    for row in ordered:
        identity = order_key(row["demand"])
        need = max(0, row["actual"] - sum(p["quantity"] for p in parts[identity].values()))
        for claim in previous.get(identity, {}).get("arrival_claims", []):
            lot = lots_by_id.get(claim["id"])
            retained = parts[identity].get((claim["id"], True), {}).get("quantity", 0.0)
            if lot is not None:
                need -= take(row, lot, min(need, max(0, claim["quantity"] - retained)), True)

    snapshots = {}
    for row in ordered:
        demand = row["demand"]
        key = demand.key
        identity = order_key(demand)
        prior = previous.get(identity)
        full = row["actual"] + allocation.EPSILON >= demand.quantity
        was_full = bool(prior and prior["actual"] + allocation.EPSILON >= prior["quantity"])
        unchanged_ready = was_full and full and demand.quantity <= prior["quantity"] + allocation.EPSILON
        steady_inventory = unchanged_ready and prior["route"] == "inventory"
        # Unknown/pre-existing stock is not evidence of a receipt for this order.
        need = max(0, row["actual"] - sum(p["quantity"] for p in parts[identity].values()))
        older_stock = min(unknown[key], need)
        unknown[key] -= older_stock
        need -= older_stock
        for lot in queues[key]:
            arrival = (not steady_inventory or receipt_lag_proven(prior, lot)) and dt.date.fromisoformat(lot["date"]) >= demand.date
            need -= take(row, lot, need, arrival)
            if need <= allocation.EPSILON:
                break
        if need > allocation.EPSILON:
            raise ValueError(f"订单现货来源数量不足: {demand.order}")
        matches, receipts = [], []
        for part in parts[identity].values():
            lot = state["events"][part["id"]]
            receipt_date = dt.date.fromisoformat(lot["date"])
            age = (today - receipt_date).days
            evidence = {"event_id":lot["id"], "document": lot["document"], "date": lot["date"], "days": age, "quantity": part["quantity"],
                        "type":lot["type"], "supplier_id":lot.get("supplier_id", ""), "supplier_name":lot.get("supplier_name", "")}
            if part["arrival"]:
                receipts.append(evidence)
                if age > WARNING_DAYS:
                    matches.append(evidence)
        # Provenance is a durable fact; physical/FIFO observations can shrink
        # its current allocation but must not silently erase it. Claims never
        # replenish lot balances or grant physical stock. Explicitly voided
        # inbound events remove their claims; completed orders are removed by
        # release_receipt_orders before this calculation.
        claims = {}
        for part in (prior or {}).get("arrival_claims", []) + [
                p for p in (prior or {}).get("receipts", []) if p["arrival"]]:
            lot = state["events"].get(part["id"])
            if lot and lot["quantity"] > allocation.EPSILON:
                claims[part["id"]] = max(claims.get(part["id"], 0.0), part["quantity"])
        for part in parts[identity].values():
            if part["arrival"]:
                claims[part["id"]] = max(claims.get(part["id"], 0.0), part["quantity"])
        # A genuine reduction in open demand reduces the retained claim scope.
        # Historical claims take priority over newly observed replacement lots.
        claim_budget = demand.quantity
        retained_claims = []
        for lot_id, amount in claims.items():
            assigned = parts[identity].get((lot_id, True), {}).get("quantity", 0.0)
            missing = max(0, amount - assigned)
            # Only an explicit ERP inbound correction or return can retire a
            # prior arrival claim. A low physical observation cannot do this.
            retired = min(missing, source_reductions.get(lot_id, 0.0))
            source_reductions[lot_id] = source_reductions.get(lot_id, 0.0) - retired
            amount -= retired
            missing -= retired
            returned = min(missing, return_reductions.get(key, 0.0))
            return_reductions[key] = return_reductions.get(key, 0.0) - returned
            amount -= returned
            amount = min(amount, claim_budget)
            if amount <= allocation.EPSILON:
                continue
            retained_claims.append({"id": lot_id, "quantity": amount})
            claim_budget -= amount
        # Physical reductions can consume an old lot while later verified
        # arrivals cover the same demand. Preserve the historical claims, but
        # accept current, exclusively allocated arrival lots as replacement
        # evidence. Unknown stock or pre-order inventory cannot cover a claim;
        # neither can another order's retained receipt allocation.
        claimed = sum(p['quantity'] for p in retained_claims)
        covered = sum(p['quantity'] for p in receipts)
        if full and covered + allocation.EPSILON < claimed:
            raise ValueError("历史来货来源存在缺口，齐货分工待核对")
        route = None
        if full:
            # Dispatch follows the receipt provenance actually allocated above,
            # not whether the ERP returned a newly increased receipt this poll.
            # A sibling order's shipment can temporarily remove this order's
            # FIFO quota before its demand disappears; recovering the same lot
            # is still arrival work. This also repairs older ledgers whose route
            # says inventory while their retained parts explicitly say arrival.
            # steady_inventory still prevents unrelated later receipts from
            # reassigning an already-ready inventory order.
            route = "arrival" if receipts else "inventory"
            row["category"] = "来货件" if route == "arrival" else allocation.inventory_category(demand.product_code or demand.sku)
        row["normal_category"] = row["category"]
        row["receipt_allocations"] = receipts
        row["new_receipt_quantity"] = new_allocated[identity]
        row["inventory_quantity"] = row["actual"] - sum(p["quantity"] for p in receipts)
        row["dispatch_route"] = route
        row["receipt_warning"] = matches
        # An old partial receipt is useful evidence, but the order cannot be
        # packed while any quantity is still missing. Keep its purchase or
        # incoming-supply classification until this demand is physically full.
        if matches and full and route == "arrival":
            row["category"] = "异常"
        snapshots[identity] = {"quantity": demand.quantity, "actual": row["actual"],
                               "route": route, "receipts": list(parts[identity].values())}
        if retained_claims:
            snapshots[identity]["arrival_claims"] = retained_claims
        if route == "inventory":
            for target, source in (("inventory_ready_observed_at", "stock_observed_at"),
                                   ("inventory_ready_receipt_covered_through", "receipt_covered_through")):
                # Preserve the first ready observation, never replace it with
                # a newer poll and accidentally authorize unrelated receipts.
                value = (prior or {}).get(target) if steady_inventory else row.get(source)
                if observed_time(value):
                    snapshots[identity][target] = value
    allocation.assign_order_categories(output)
    state["dispatch_version"] = 1
    state["orders"] = snapshots
    return output
