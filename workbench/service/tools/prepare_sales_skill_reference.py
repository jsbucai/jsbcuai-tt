"""Prepare a new private sales-history reference for a verified live profile.

Reads the frozen NAS source and current bindings only. It never imports into
the active business DB, queries ERP or changes an existing output. Matching
rows whose historical customer identity is ambiguous are retained in review.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.sales_history_matching import file_sha256, import_history_snapshot


def _text(value):
    return '' if value is None else str(value).strip()


def _digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _private_file(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush(); os.fsync(output.fileno())


def _read_scope(current_db):
    db = sqlite3.connect(current_db.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        row = db.execute("SELECT value FROM settings WHERE key='sales_web_import_policy'").fetchone()
        if not row:
            raise ValueError('sales_reference_verified_customer_policy_missing')
        policy = json.loads(row[0])
        profile = _text(policy.get('profile_id'))
        account = _text(policy.get('account_id'))
        bindings = policy.get('customer_bindings')
        if not profile or not account or not isinstance(bindings, dict) or not bindings:
            raise ValueError('sales_reference_customer_policy_invalid')
        profiles = {_text(json.loads(row[0]).get('profile_id')) for row in db.execute(
            "SELECT payload FROM records WHERE kind='inventory'")}
        if profiles != {profile}:
            raise ValueError('sales_reference_current_profile_mismatch')
        identities = defaultdict(list)
        validated = {}
        for name, binding in bindings.items():
            if (not isinstance(name, str) or not name or name != name.strip() or
                    not isinstance(binding, dict) or binding.get('customer_name') != name or
                    binding.get('profile_id') != profile or binding.get('account_id') != account or
                    not _text(binding.get('erp_customer_id'))):
                raise ValueError('sales_reference_customer_binding_invalid')
            identities[_text(binding['erp_customer_id'])].append(name)
            validated[name] = binding
        duplicate_ids = {identity for identity, names in identities.items() if len(names) != 1}
        return profile, account, {name: binding for name, binding in validated.items()
                                 if _text(binding['erp_customer_id']) not in duplicate_ids}
    finally:
        db.close()


def prepare_reference(source_path, expected_sha256, current_db_path, output_path):
    source = Path(source_path)
    current = Path(current_db_path)
    output = Path(output_path)
    for path in (source, current, output):
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError('sales_reference_absolute_nonsymlink_path_required')
    if output.exists() or output == source or output == current or output.name != 'sales-matching.sqlite3':
        raise ValueError('sales_reference_new_independent_output_required')
    if output.is_relative_to(Path('/mnt/nas')):
        raise ValueError('sales_reference_active_nas_output_rejected')
    if output.parent.exists():
        raise ValueError('sales_reference_new_private_directory_required')
    if not source.is_file() or not current.is_file() or file_sha256(source) != expected_sha256:
        raise ValueError('sales_reference_source_hash_mismatch')
    profile, account, bindings = _read_scope(current)
    if not bindings:
        raise ValueError('sales_reference_customer_bindings_empty')
    source_db = sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        source_db.execute('PRAGMA query_only=ON')
        names_by_code = defaultdict(set)
        codes_by_name = defaultdict(set)
        for name, code in source_db.execute('SELECT DISTINCT customer_name,customer_code FROM historical_orders'):
            name, code = _text(name), _text(code)
            if name and code:
                names_by_code[code].add(name); codes_by_name[name].add(code)
        allowed = {}
        proofs = {_digest(name): {'erp_customer_id_sha256': _digest(binding['erp_customer_id']),
            'old_customer_code_sha256': '', 'historical_customer_linked': False,
            'account_id': account, 'profile_id': profile} for name, binding in bindings.items()}
        for name, binding in bindings.items():
            codes = codes_by_name.get(name, set())
            if len(codes) != 1:
                continue
            code = next(iter(codes))
            if names_by_code[code] != {name}:
                continue
            allowed[name] = code
            proofs[_digest(name)] = {'erp_customer_id_sha256': _digest(binding['erp_customer_id']),
                'old_customer_code_sha256': _digest(code), 'historical_customer_linked': True,
                'account_id': account, 'profile_id': profile}
        if not allowed:
            raise ValueError('sales_reference_unique_current_customer_links_empty')
        output.parent.mkdir(mode=0o700, parents=True)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        destination = sqlite3.connect(output)
        try:
            destination.execute('PRAGMA journal_mode=DELETE')
            metadata = import_history_snapshot(destination, source, profile, expected_sha256)
            # Every retained row is independently linked by old code and exact
            # name. Neither name fuzzy matching nor address aliases are used.
            unbound = []
            for row_id, name, code in source_db.execute('SELECT order_row_id,customer_name,customer_code FROM historical_orders'):
                if allowed.get(_text(name)) != _text(code):
                    unbound.append(_text(row_id))
            for row_id in unbound:
                row = destination.execute('SELECT review_reasons FROM sales_matching_history WHERE profile_id=? '
                    'AND source_sha256=? AND source_row_id=?', (profile, expected_sha256, row_id)).fetchone()
                reasons = sorted(set(json.loads(row[0]) + ['historical_customer_identity_unbound']))
                destination.execute('UPDATE sales_matching_history SET eligible=0,review_reasons=? WHERE profile_id=? '
                    'AND source_sha256=? AND source_row_id=?',
                    (json.dumps(reasons), profile, expected_sha256, row_id))
            eligible = destination.execute('SELECT count(*) FROM sales_matching_history WHERE eligible=1').fetchone()[0]
            metadata = {**metadata, 'eligible_rows': eligible, 'review_rows': metadata['imported_rows'] - eligible,
                'customer_identity_link_method': 'exact_unique_verified_current_name_and_unique_historical_code',
                'bound_customer_count': len(allowed), 'unbound_history_rows': len(unbound),
                'verified_current_customer_count': len(bindings),
                'review_reason_counts': {**metadata['review_reason_counts'],
                                        'historical_customer_identity_unbound': len(unbound)},
                'customer_binding_proofs': proofs,
                'customer_bindings_sha256': _digest(json.dumps(proofs, sort_keys=True, separators=(',', ':')))}
            destination.execute('UPDATE sales_matching_sources SET metadata=? WHERE profile_id=? AND source_sha256=?',
                (json.dumps(metadata, ensure_ascii=False), profile, expected_sha256))
            destination.commit()
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('sales_reference_integrity_failed')
        finally:
            destination.close()
        if file_sha256(source) != expected_sha256:
            raise ValueError('sales_reference_source_changed')
        os.chmod(output, 0o400)
        report = {key: value for key, value in metadata.items() if key != 'customer_binding_proofs'}
        report.update(schema='ul_sales_history_reference_preparation_v1', prepared_at=datetime.now(timezone.utc).isoformat(),
                      reference_sha256=file_sha256(output), reference_bytes=output.stat().st_size,
                      source_changed=False, business_db_written=False, erp_written=False)
        _private_file(output.parent / 'preparation-report.json', report)
        return report
    finally:
        source_db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--current-db', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        report = prepare_reference(args.source, args.source_sha256, args.current_db, args.output)
        print(json.dumps({key: report[key] for key in ('reference_sha256', 'imported_rows', 'eligible_rows',
            'review_rows', 'bound_customer_count', 'unbound_history_rows', 'business_db_written', 'erp_written')}, ensure_ascii=False))
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({'status': 'blocked', 'code': 'sales_reference_preparation_failed'}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
