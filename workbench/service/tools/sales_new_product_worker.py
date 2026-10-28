#!/usr/bin/env python3
"""One durable new-product prepare/create task outside HTTP and Codex.

Creation is opt-in on this host and still requires a frozen administrator
confirmation. --readonly-preflight performs no queue claim, product write or
authorization refresh; stdin provides an optional synthetic/source suggestion.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.erp_api import ErpUnavailable
from core.sales_new_product import (ProductCreationClient,
    execute_confirmed_product_creation, fetch_new_product_context,
    prepare_new_product_plan)
from core.sales_new_product_jobs import SalesNewProductJobs
from core.sales_skill_matching import load_skill_rules
from core.store import Store, dumps


class ReadOnlyProductClient(ProductCreationClient):
    def _refresh(self, record):
        raise ErpUnavailable('new_product_readonly_authorization_expired')

    def create_product(self, plan, confirmed_plan_hash):
        raise ErpUnavailable('new_product_readonly_write_forbidden')


def _vault(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('new_product_vault_path_invalid')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError('new_product_vault_not_private')
    return path


def _reserved_codes(store, excluded):
    with store.connect() as db:
        rows = db.execute("""SELECT plan_json FROM sales_new_product_plans WHERE id!=?
            AND status IN ('ready_for_confirmation','queued','creating','unknown','created')""", (excluded,)).fetchall()
    output = []
    for row in rows:
        code = json.loads(row[0]).get('code')
        if code:
            output.append(code)
    return output


def run_one(store, client, rules_root, *, allow_create=False):
    jobs = SalesNewProductJobs(store)
    token = uuid.uuid4().hex
    snapshot = jobs.claim(token, allow_create=allow_create)
    if snapshot is None:
        return {'claimed': False, 'external_submit': False}
    identity = snapshot['id']
    try:
        if snapshot['status'] == 'preparing':
            rules = load_skill_rules(Path(rules_root) / 'references/rules')
            context = fetch_new_product_context(client, snapshot['profile_id'])
            plan = prepare_new_product_plan(snapshot['suggestion'],
                profile_id=snapshot['profile_id'], data_version=snapshot['data_version'],
                **context, reference_code=snapshot.get('reference_code', ''),
                reserved_codes=_reserved_codes(store, identity), rules=rules)
            result = jobs.finish_prepare(identity, token, plan)
            return {'claimed': True, **result, 'external_submit': False}
        if snapshot['status'] != 'creating' or allow_create is not True:
            raise ValueError('new_product_worker_phase_forbidden')
        outcome = execute_confirmed_product_creation(client, snapshot['plan'],
            confirmed_plan_hash=snapshot['plan_hash'],
            pre_submit_check=lambda: jobs.authorize_submission(identity, token))
        result = jobs.finish_creation(identity, token, status=outcome['status'],
            result=outcome['result'], error_code=outcome['result'].get('reason', ''))
        return {'claimed': True, **result, 'external_submit': outcome['status'] != 'blocked'}
    except Exception as error:
        code = getattr(error, 'code', '')
        if not isinstance(code, str) or not code.startswith(('sales_new_product_', 'new_product_')):
            code = str(error) if isinstance(error, ValueError) and str(error).startswith('new_product_') else 'new_product_worker_failed'
        try:
            jobs.fail(identity, token, code)
        except Exception:
            code = 'new_product_claim_changed'
        return {'claimed': True, 'id': identity,
            'status': 'unknown' if snapshot['status'] == 'creating' else 'failed',
            'code': code, 'external_submit': snapshot['status'] == 'creating'}


def readonly_preflight(client, profile_id, *, suggestion=None, reference_code='', data_version='readonly-preflight', rules=None):
    """Validate all three documented read contracts without claiming a job."""
    context = fetch_new_product_context(client, profile_id)
    candidates = sorted(row.get('usercode', '') for row in context['products'] if row.get('usercode'))
    query = reference_code or (candidates[0] if candidates else '')
    if not query:
        raise ValueError('new_product_preflight_catalog_empty')
    exact = client.call('erp.ptype.listbykey', {'keyword': query})
    found = exact.get('ptypes') if isinstance(exact, dict) else None
    if not isinstance(found, list) or len([row for row in found if isinstance(row, dict)
            and str(row.get('usercode', '')).upper() == query.upper()]) != 1:
        raise ValueError('new_product_preflight_exact_contract_unverified')
    output = {'status': 'read_contract_verified', 'profile_matches': True,
        'product_count': len(context['products']), 'class_count': len(context['classes']),
        'inactive_deleted_count': sum(str(row.get('deleted', '')).lower() in {'true', '1'} or
            str(row.get('isstop', '')).lower() in {'true', '1'} for row in context['products']),
        'exact_code_matches': 1, 'external_submit': False}
    if suggestion is not None:
        plan = prepare_new_product_plan(suggestion, profile_id=profile_id,
            data_version=data_version, **context, reference_code=reference_code, rules=rules)
        output['plan_status'] = plan['status']
        output['issues'] = plan['issues']
        output['template_reference_count'] = len(plan['reference_choices'])
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--rules-root', required=True, type=Path)
    parser.add_argument('--runtime')
    parser.add_argument('--allow-create', action='store_true', help='host deployment gate; admin confirmation is still mandatory')
    parser.add_argument('--readonly-preflight', action='store_true')
    parser.add_argument('--profile-id', help='required for read-only preflight')
    args = parser.parse_args(argv)
    try:
        vault = _vault(args.vault)
        rules = load_skill_rules(args.rules_root / 'references/rules')
        if args.readonly_preflight:
            if args.allow_create or not args.profile_id:
                raise ValueError('new_product_preflight_scope_invalid')
            packet = json.load(sys.stdin) if not sys.stdin.isatty() else {}
            if not isinstance(packet, dict):
                raise ValueError('new_product_preflight_input_invalid')
            result = readonly_preflight(ReadOnlyProductClient(vault), args.profile_id,
                suggestion=packet.get('suggestion'), reference_code=packet.get('reference_code', ''), rules=rules)
        else:
            if not args.runtime:
                raise ValueError('new_product_runtime_missing')
            result = run_one(Store(args.runtime), ProductCreationClient(vault), args.rules_root,
                allow_create=args.allow_create)
        print(dumps(result))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and str(error).startswith('new_product_') else 'new_product_worker_unavailable'
        print(dumps({'claimed': False, 'status': 'failed', 'code': code, 'external_submit': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
