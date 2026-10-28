import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.receipt_bootstrap import convert_receipt_state


OLD_EVENT = "legacy-event"


def fixtures():
    raw = {
        "version": 1,
        "dispatch_version": 1,
        "period_start": "2026-08-24",
        "period_end": "2026-09-24",
        "events": {OLD_EVENT: {
            "id": OLD_EVENT,
            "document": "IN1",
            "type": "进货入库单",
            "warehouse": "主仓",
            "sku": "LS01",
            "date": "2026-09-24",
            "quantity": 5,
        }},
        "balances": {OLD_EVENT: 0},
        "orders": {
            json.dumps(["A#1", "主仓", "LS01", "2026-09-20"], ensure_ascii=False): {
                "quantity": 5,
                "actual": 5,
                "route": "arrival",
                "receipts": [{"id": OLD_EVENT, "quantity": 5, "arrival": True}],
            }
        },
    }
    mapping = {
        "matched_events": 1,
        "unmatched_events": 0,
        "mappings": [{
            "legacy_event_id": OLD_EVENT,
            "quantity_equal": True,
            "api_identities": [{
                "warehouse_id": "20",
                "product_id": "30",
                "sku_id": "31",
            }],
        }],
    }
    inventory = [{
        "profile_id": "10",
        "warehouse_id": "20",
        "product_id": "30",
        "sku_id": "31",
        "base_unit": "1",
        "sku": "LS01",
    }]
    return raw, mapping, inventory


def test_verified_legacy_state_converts_all_event_and_order_references():
    raw, mapping, inventory = fixtures()
    state = convert_receipt_state(raw, mapping, inventory, "10", "20")
    assert state["profile_id"] == "10"
    assert len(state["events"]) == len(state["balances"]) == len(state["orders"]) == 1
    event_id = next(iter(state["events"]))
    order = next(iter(state["orders"].values()))
    assert event_id != OLD_EVENT
    assert order["receipts"][0]["id"] == event_id
    assert state["events"][event_id]["sku"] == "30"
    assert json.loads(state["events"][event_id]["warehouse"]) == ["10", "20", "31", "1"]
    assert len(state["scope_periods"]) == 1


def test_bootstrap_rejects_unverified_quantity_mapping():
    raw, mapping, inventory = fixtures()
    broken = copy.deepcopy(mapping)
    broken["mappings"][0]["quantity_equal"] = False
    with pytest.raises(ValueError, match="数量未与 API 闭合"):
        convert_receipt_state(raw, broken, inventory, "10", "20")
