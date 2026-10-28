"""Narrow host-only authorization for one sales acceptance order.

This does not mark production acceptance complete. The private host config
must name one frozen confirmation, and the generated XLS total is checked
separately before the transport can make its sole upload attempt.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping


TRIAL_SCHEMA = "sales_web_trial_v1"
LINE_TRIAL_SCHEMA = "sales_web_trial_v2"
TRIAL_FIELDS = frozenset({"schema", "session_id", "confirmed_hash",
    "order_number", "customer_name", "line_count", "taxed_total"})
LINE_TRIAL_FIELDS = TRIAL_FIELDS | {'payload_schema', 'document_mode', 'document_numbers'}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]{2}\Z")


def sales_write_mode(enabled, acceptance_complete, trial_authorization):
    """Return a mode only for two unambiguous, explicitly configured states."""
    if type(enabled) is not bool or type(acceptance_complete) is not bool:
        return "disabled"
    if enabled and acceptance_complete and trial_authorization is None:
        return "production"
    if not enabled and not acceptance_complete and isinstance(trial_authorization, Mapping):
        return "trial"
    return "disabled"


def trial_request_matches(trial, payload, operation):
    """Bind the host's one trial to the queued immutable confirmation."""
    if (not isinstance(trial, Mapping) or
            not isinstance(payload, Mapping) or not isinstance(operation, Mapping)):
        return False
    try:
        line_documents = trial.get('schema') == LINE_TRIAL_SCHEMA
        if set(trial) != (LINE_TRIAL_FIELDS if line_documents else TRIAL_FIELDS):
            return False
        if line_documents:
            numbers = trial['document_numbers']
            if (trial['payload_schema'] != 'sales_web_import_v2' or
                    payload.get('schema') != 'sales_web_import_v2' or
                    trial['document_mode'] != 'line_document' or
                    payload.get('document_mode') != 'line_document' or
                    not isinstance(numbers, list) or not 1 <= len(numbers) <= 500 or
                    any(not isinstance(number, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', number)
                        for number in numbers) or len(set(numbers)) != len(numbers) or
                    numbers != payload.get('document_numbers') or
                    numbers[0] != trial['order_number'] or len(numbers) != trial['line_count']):
                return False
        elif trial.get('schema') != TRIAL_SCHEMA or payload.get('schema') != 'sales_web_import_v1':
            # A previous one-document approval never authorizes a new
            # multi-document import, even if its first number/hash match.
            return False
        session = payload["frozen_session"]
        draft = json.loads(session["draft"])
        count = trial["line_count"]
        gross = trial["taxed_total"]
        return (trial["schema"] == (LINE_TRIAL_SCHEMA if line_documents else TRIAL_SCHEMA) and
            isinstance(trial["session_id"], str) and bool(trial["session_id"]) and
            isinstance(trial["confirmed_hash"], str) and
            _HASH.fullmatch(trial["confirmed_hash"]) is not None and
            isinstance(trial["order_number"], str) and bool(trial["order_number"]) and
            isinstance(trial["customer_name"], str) and bool(trial["customer_name"]) and
            type(count) is int and count > 0 and
            isinstance(gross, str) and _MONEY.fullmatch(gross) is not None and
            gross != "0.00" and
            operation.get("kind") == "sales_order_import" and
            operation.get("role") == "sales" and
            trial["session_id"] == payload.get("session_id") == session.get("id") and
            trial["confirmed_hash"] == payload.get("confirmed_hash") == session.get("confirmed_hash") and
            trial["order_number"] == payload.get("order_number") == operation.get("target_id") and
            trial["customer_name"] == payload.get("customer_name") == session.get("customer_name") == draft.get("customer_name") and
            count == payload.get("line_count") == len(draft["lines"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
