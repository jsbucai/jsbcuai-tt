"""Prepare and verify a single confirmed ERP product creation.

The NAS number sequence is reused against a fresh *complete* API catalog. A
model may suggest attributes, but it never supplies a code, category or ERP
payload. The caller owns durable claims, reservations and admin confirmation.
This module performs no database writes and never retries a product mutation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import re
import unicodedata

from .erp_api import ErpClient, ErpUnavailable
from .erp_source import BUSINESS_WAREHOUSE_ID, END, START, stable_id, variant_id
from .sales_skill_matching import catalog_attributes, _family, _size as _normalized_size, SIZE_RE
from .sales_codex_pipeline import digest
from .unit_policy import CONFIRMED_BASE_UNIT, unit_evidence
from .wsgjp_gateway import ApiError, business_signature, compact


SCHEMA_VERSION = 1
MAX_PRODUCTS = 50000
MAX_CLASSES = 10000
PAYLOAD_FIELDS = frozenset({
    'ParTypeId', 'FullName', 'Name', 'UserCode', 'Standard', 'Type', 'Area',
    'Unit', 'BarCode', 'BrandName', 'Comment',
})
PRODUCT_READ_METHODS = frozenset({'erp.ptype.listbykey', 'erp.ptype.classlist'})


def _text(value):
    return '' if value is None else str(value).strip()


def _key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', _text(value))).upper()


def _size(value):
    # Formatting only: never remove a pitch, length, material or standard.
    return _normalized_size(value)


def _standard(value):
    return _key(value).replace('GB/T', 'GB').replace('GBT', 'GB')


def _yes(value):
    return value is True or (not isinstance(value, bool) and _text(value).lower() in {'1', 'true', '是'})


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def new_product_plan_hash(plan):
    """Bind confirmation to every source, issue and payload field."""
    # The durable queue adds session/suggestion/result/version bindings. Use
    # exactly its insertion-preserving digest over the entire frozen object.
    return digest(plan)


def _product(raw):
    """Keep raw API parent and identities; derive only explicit attributes."""
    source = {
        'product_name': _text(raw.get('fullname') or raw.get('product_name')),
        'spec': _text(raw.get('standard') if 'standard' in raw else raw.get('spec')),
        'model': _text(raw.get('type') if 'type' in raw else raw.get('model')),
        'material': _text(raw.get('area') if 'area' in raw else raw.get('material')),
        'standard_no': _text(raw.get('standard_no')),
        'grade': _text(raw.get('grade')), 'surface': _text(raw.get('surface')),
    }
    attrs = catalog_attributes(source)
    # Catalog full names are allowed to contain an explicit standard number.
    from .sales_skill_matching import STANDARD_RE
    standards = {match.group().strip() for match in STANDARD_RE.finditer(source['product_name'])}
    if not attrs['standard'] and len(standards) == 1:
        attrs['standard'] = standards.pop()
    return {
        **attrs, 'code': _text(raw.get('usercode') or raw.get('sku')).upper(),
        'product_id': _text(raw.get('ptypeid') or raw.get('product_id')),
        'parent_id': _text(raw.get('parptypeid') or raw.get('parent_category_id')),
        'short_name': _text(raw.get('name') or source['product_name']),
        'active': not any(_yes(raw.get(field)) for field in ('isstop', 'deleted', 'isclass')),
        'raw': raw,
    }


def classification_choices(classes, profile_id):
    """Return only unique enabled complete paths and their real TypeId."""
    if not isinstance(classes, list) or len(classes) > MAX_CLASSES or any(not isinstance(x, dict) for x in classes):
        raise ValueError('new_product_classes_invalid')
    by_id, duplicates = {}, set()
    for row in classes:
        # Real classlist on 2026-10-07 returns profileid='0' for all 191
        # classifications. It is a row sentinel, not the authorization scope;
        # the caller binds account() and current product-parent identities.
        if _text(row.get('profileid')) not in ('', '0', _text(profile_id)):
            raise ValueError('new_product_class_profile_conflict')
        key = _text(row.get('id') or row.get('category_id'))
        if not key:
            raise ValueError('new_product_class_identity_missing')
        if key in by_id:
            duplicates.add(key)
        by_id[key] = {
            'id': key, 'type_id': _text(row.get('typeid') or row.get('type_id')),
            'parent_id': _text(row.get('parid') or row.get('parent_category_id')),
            'name': _text(row.get('fullname') or row.get('full_name')),
            'active': not any(_yes(row.get(field)) for field in ('isstop', 'deleted', 'is_stopped', 'is_deleted')),
        }
    paths = defaultdict(list)
    for key, row in by_id.items():
        names, seen, current, valid = [], set(), key, True
        while current and current not in {'0', '-1'}:
            item = by_id.get(current)
            if item is None or current in seen or current in duplicates or not item['active'] or not item['name']:
                valid = False
                break
            seen.add(current)
            if item['name'] not in {'库存商品', '全部分类'}:
                names.append(item['name'])
            current = item['parent_id']
        if valid and names and row['type_id']:
            path = list(reversed(names))
            paths[tuple(path)].append({**row, 'path': path, 'classification_path': ' / '.join(path)})
    type_counts = defaultdict(int)
    for items in paths.values():
        for item in items:
            type_counts[item['type_id']] += 1
    return sorted((item for items in paths.values() if len(items) == 1
        for item in items if type_counts[item['type_id']] == 1),
        key=lambda item: (item['classification_path'], item['id']))


def _pattern(product, category):
    match = re.fullmatch(r'([A-Z]+)(\d+)', product['code'])
    if not match:
        return None
    return (product['product_name'], product['short_name'], product['model'],
        product['material'], category['type_id'], category['classification_path'], match.group(1))


def _next_code(reference_code, products, reserved_codes):
    """NAS max+1 allocation, including stopped/deleted products and reservations."""
    match = re.fullmatch(r'([A-Z]+)(\d+)', reference_code)
    if not match:
        raise ValueError('new_product_reference_prefix_invalid')
    prefix, width = match.group(1), max(4, len(match.group(2)))
    numbers = []
    for code in [p['code'] for p in products] + [_text(x).upper() for x in reserved_codes]:
        other = re.fullmatch(re.escape(prefix) + r'(\d+)', code)
        if other:
            numbers.append(int(other.group(1)))
            width = max(width, len(other.group(1)))
    if not numbers:
        raise ValueError('new_product_reference_prefix_unverified')
    return prefix + str(max(numbers) + 1).zfill(width)


def prepare_new_product_plan(suggestion, *, profile_id, data_version, products,
        classes, completeness, reference_code='', reserved_codes=(), rules=None):
    """Build a frozen candidate from server-owned API rows, never model codes.

    `completeness` must prove a fresh full `erp.ptype.list(isall=true)` result,
    including stopped/deleted number occupation, and a whole classlist. The
    HTTP layer must not accept these rows/proofs from the phone. A caller may
    select a *current* reference code; otherwise one unique template is needed.
    Reserve `code` in the caller's transaction before publishing this plan.
    """
    if not isinstance(suggestion, dict):
        raise ValueError('new_product_suggestion_invalid')
    if not isinstance(products, list) or len(products) > MAX_PRODUCTS or any(not isinstance(x, dict) for x in products):
        raise ValueError('new_product_catalog_invalid')
    profile_id, data_version = _text(profile_id), _text(data_version)
    if not profile_id or not data_version:
        raise ValueError('new_product_source_version_missing')
    if not isinstance(completeness, dict) or not (
            completeness.get('products_complete') is True and
            completeness.get('classes_complete') is True and
            completeness.get('includes_inactive_deleted') is True and
            _text(completeness.get('profile_id')) == profile_id and
            completeness.get('product_count') == len(products)):
        raise ValueError('new_product_catalog_not_complete')
    if any(_text(row.get('profileid') or row.get('profile_id')) not in ('', profile_id) for row in products):
        raise ValueError('new_product_catalog_profile_conflict')
    source = {field: _text(suggestion.get(field)) for field in (
        'product_name', 'spec', 'standard', 'grade', 'surface', 'material', 'source_quote')}
    source['source_ids'] = suggestion.get('source_ids', [])
    if (not isinstance(source['source_ids'], list) or not source['source_ids'] or
            any(not isinstance(x, str) or not x.strip() for x in source['source_ids']) or
            len(set(source['source_ids'])) != len(source['source_ids'])):
        raise ValueError('new_product_source_ids_invalid')
    if any('\x00' in value or len(value) > 500 for key, value in source.items() if key not in {'source_quote', 'source_ids'}):
        raise ValueError('new_product_source_field_invalid')
    current = [_product(row) for row in products]
    identities = [p['product_id'] for p in current]
    if not all(identities) or len(set(identities)) != len(identities):
        raise ValueError('new_product_catalog_identity_conflict')
    categories = classification_choices(classes, profile_id)
    category_map = {item['id']: item for item in categories}
    plan = {
        'schema_version': SCHEMA_VERSION, 'status': 'blocked',
        'profile_id': profile_id, 'catalog_version': data_version,
        'product_name': source['product_name'], 'spec': source['spec'], 'model': '',
        'code': '', 'base_unit': CONFIRMED_BASE_UNIT, 'classification_path': '',
        'classification_id': '',
        'reference_code': '', 'issues': [], 'payload': {},
        'source_fingerprint': _hash({'products': products, 'classes': classes, 'completeness': completeness}),
        'identity_hash': '', 'source': source, 'reference_choices': [],
        'classification_choices': [], 'existing_codes': [],
    }
    issues = plan['issues']
    for field in ('product_name', 'spec', 'source_quote'):
        if not source[field]:
            issues.append('new_product_' + field + '_missing')
    if _text(suggestion.get('base_unit')) not in ('', CONFIRMED_BASE_UNIT):
        issues.append('new_product_unit_conflict')
    family = _family(source['product_name'], rules)[0]
    potential = []
    for product in current:
        if not product['active'] or not product['code'] or product['attribute_review_reasons']:
            continue
        if _family(product['product_name'], rules)[0] != family:
            continue
        # Every present template attribute must have explicit source evidence.
        checks = (
            (_standard(source['standard']), _standard(product['standard'])),
            (_key(source['grade']).removesuffix('级'), _key(product['grade']).removesuffix('级')),
            (_key(source['surface']), _key(product['surface'])),
            (_key(source['material']), _key(product['material'])),
        )
        if any(left != right for left, right in checks):
            continue
        category = category_map.get(product['parent_id'])
        if category is None:
            continue
        pattern = _pattern(product, category)
        if pattern is not None:
            potential.append((product, category, pattern))
    plan['reference_choices'] = [{
        'code': p['code'], 'product_name': p['product_name'], 'spec': p['spec'],
        'model': p['model'], 'material': p['material'],
        'classification_path': c['classification_path'],
    } for p, c, _ in sorted(potential, key=lambda item: item[0]['code'])[:30]]
    selected = None
    reference_code = _text(reference_code).upper()
    if reference_code:
        matches = [item for item in potential if item[0]['code'] == reference_code]
        if len(matches) == 1:
            selected = matches[0]
        else:
            issues.append('new_product_reference_not_unique_or_attributes_conflict')
    elif len({item[2] for item in potential}) == 1:
        selected = sorted(potential, key=lambda item: item[0]['code'])[0]
    else:
        issues.append('new_product_reference_template_not_unique')
    if selected is not None:
        reference, category, _ = selected
        plan.update(product_name=reference['product_name'], model=reference['model'],
            reference_code=reference['code'], classification_path=category['classification_path'],
            classification_id=category['id'])
        if any(SIZE_RE.search(reference[field]) for field in ('product_name', 'short_name')):
            issues.append('new_product_reference_name_contains_spec')
        try:
            plan['code'] = _next_code(reference['code'], current, reserved_codes)
        except ValueError as error:
            issues.append(str(error))
        payload = {
            'ParTypeId': category['type_id'], 'FullName': reference['product_name'],
            'Name': reference['short_name'], 'UserCode': plan['code'],
            'Standard': source['spec'], 'Type': reference['model'],
            'Area': reference['material'], 'Unit': CONFIRMED_BASE_UNIT,
            'BarCode': plan['code'],
        }
        plan['payload'] = {key: value for key, value in payload.items() if value}
        identity = {key: _key(payload.get(key)) for key in ('FullName', 'Standard', 'Type', 'Area')}
        identity['Standard'] = _size(payload['Standard'])
        plan['identity_hash'] = _hash(identity)
        # Scan the entire verified catalog, not just the five recall candidates.
        duplicate = [p['code'] for p, _, _ in potential if _size(p['spec']) == identity['Standard']]
        duplicate += [p['code'] for p in current if p['active'] and
            _key(p['product_name']) == identity['FullName'] and
            _size(p['spec']) == identity['Standard'] and
            _key(p['model']) == identity['Type'] and _key(p['material']) == identity['Area']]
        if duplicate:
            plan['existing_codes'] = sorted(set(duplicate))
            issues.append('new_product_identity_already_exists')
        if any(p['code'] == plan['code'] for p in current) or plan['code'] in {_text(x).upper() for x in reserved_codes}:
            issues.append('new_product_code_already_exists')
    plan['issues'] = sorted(set(issues))
    if not plan['issues'] and plan['payload']:
        plan['status'] = 'ready'
    return plan


class ProductCreationClient(ErpClient):
    """Separate narrow write adapter; the general ErpClient remains read-only."""

    def call(self, method, params):
        if method not in PRODUCT_READ_METHODS:
            return super().call(method, params)
        return self._product_request(method, params, write=False)

    def _product_request(self, method, params, *, write):
        if (write and method != 'erp.ptype.add') or (not write and method not in PRODUCT_READ_METHODS):
            raise ErpUnavailable('new_product_method_not_allowed')
        if {'appkey', 'token', 'method', 'timestamp', 'sign'} & params.keys():
            raise ValueError('new_product_public_parameter_override')
        with self.lock:
            record = self._ready()
            c = record['credentials']
            def send(active):
                payload = {key: compact(value) if isinstance(value, (dict, list, bool)) else str(value)
                    for key, value in params.items()}
                payload.update(appkey=c['appkey'], token=active['auth_token'], method=method,
                    timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                payload['sign'] = business_signature(payload, c['signkey'])
                return self.transport('/allnewapi', payload)
            try:
                return send(record['token'])
            except ApiError as error:
                if not write and error.code in {13, 15}:
                    try:
                        return send(self._refresh(record)['token'])
                    except Exception:
                        pass
                raise ErpUnavailable('new_product_result_unknown', error.category, error.code) from None

    def create_product(self, plan, confirmed_plan_hash):
        validate_confirmed_plan(plan, confirmed_plan_hash)
        return self._product_request('erp.ptype.add', {'ptypes': [plan['payload']]}, write=True)


def validate_confirmed_plan(plan, confirmed_plan_hash):
    if (not isinstance(plan, dict) or plan.get('schema_version') != SCHEMA_VERSION or
            plan.get('status') != 'ready' or plan.get('issues') or
            _text(confirmed_plan_hash) != new_product_plan_hash(plan)):
        raise ValueError('new_product_confirmation_conflict')
    payload = plan.get('payload')
    if (not isinstance(payload, dict) or set(payload) - PAYLOAD_FIELDS or
            any(not _text(payload.get(key)) for key in ('ParTypeId', 'FullName', 'Name', 'UserCode', 'Standard', 'Unit')) or
            payload.get('Unit') != CONFIRMED_BASE_UNIT or payload.get('UserCode') != plan.get('code') or
            not re.fullmatch(r'[A-Z]+\d{4,}', _text(plan.get('code')))):
        raise ValueError('new_product_payload_invalid')


def fetch_new_product_context(client, profile_id):
    """Read complete raw products, including deleted occupation, and classes."""
    if _text(client.account()) != _text(profile_id):
        raise ValueError('new_product_live_profile_conflict')
    products = client.pages('erp.ptype.list', {
        'begintime': START, 'endtime': END, 'timetype': 1, 'isall': True},
        'ptypes', lambda row: _text(row.get('ptypeid')), size=200, limit=300)
    if len(products) > MAX_PRODUCTS:
        raise ValueError('new_product_catalog_too_large')
    response = client.call('erp.ptype.classlist', {})
    classes = response.get('ptypeclasslist') if isinstance(response, dict) else None
    if not isinstance(classes, list):
        raise ValueError('new_product_class_response_invalid')
    classification_choices(classes, profile_id)
    return {'products': products, 'classes': classes, 'completeness': {
        'profile_id': _text(profile_id), 'products_complete': True,
        'classes_complete': True, 'includes_inactive_deleted': True,
        'product_count': len(products), 'fetched_at': datetime.now().isoformat(timespec='seconds'),
        'method': 'erp.ptype.list+erp.ptype.classlist',
    }}


def _lookup(client, code):
    result = client.call('erp.ptype.listbykey', {'keyword': code})
    rows = result.get('ptypes') if isinstance(result, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('new_product_lookup_response_invalid')
    return [row for row in rows if _text(row.get('usercode')).upper() == code]


def _outcome(status, code, **result):
    return {'status': status, 'result': {'reason': code, **result}}


def verify_created_product(client, plan, *, receipt=None):
    """Prove exact product and SKU identity; failure never invites another add."""
    try:
        if _text(client.account()) != plan['profile_id']:
            return _outcome('unknown', 'new_product_live_profile_conflict')
        matches = _lookup(client, plan['code'])
        if len(matches) != 1:
            return _outcome('unknown', 'new_product_lookup_not_unique')
        product_id = _text(matches[0].get('ptypeid') or matches[0].get('id'))
        if not product_id:
            return _outcome('unknown', 'new_product_identity_missing')
        if receipt and _text(receipt.get('id') or receipt.get('ptypeid')) != product_id:
            return _outcome('unknown', 'new_product_receipt_identity_conflict')
        # The exact-key endpoint sample has no SKU. Read the documented current
        # catalog and require this same ptypeid's actual skus; never synthesize.
        products = client.pages('erp.ptype.list', {
            'begintime': START, 'endtime': END, 'timetype': 1, 'isall': True},
            'ptypes', lambda row: _text(row.get('ptypeid')), size=200, limit=300)
        rows = [row for row in products if _text(row.get('ptypeid')) == product_id]
        if len(rows) != 1:
            return _outcome('unknown', 'new_product_catalog_identity_not_unique')
        raw, expected = rows[0], plan['payload']
        product = _product(raw)
        if (not product['active'] or product['code'] != plan['code'] or
                _key(product['product_name']) != _key(expected['FullName']) or
                _size(product['spec']) != _size(expected['Standard']) or
                _key(product['model']) != _key(expected.get('Type')) or
                _key(product['material']) != _key(expected.get('Area')) or
                product['parent_id'] != plan.get('classification_id')):
            return _outcome('unknown', 'new_product_created_attributes_conflict')
        if _text(raw.get('unit1')) != CONFIRMED_BASE_UNIT:
            return _outcome('unknown', 'new_product_created_unit_not_verified')
        skus = raw.get('skus')
        if not isinstance(skus, list) or len(skus) != 1 or not isinstance(skus[0], dict):
            return _outcome('unknown', 'new_product_created_sku_not_unique')
        sku_id = variant_id(skus[0])
        if not sku_id:
            return _outcome('unknown', 'new_product_created_sku_missing')
        if receipt and _text(receipt.get('skuid')) != sku_id:
            return _outcome('unknown', 'new_product_receipt_sku_conflict')
        ident = {'profile_id': plan['profile_id'], 'warehouse_id': BUSINESS_WAREHOUSE_ID,
            'product_id': product_id, 'sku_id': sku_id, 'base_unit': '1'}
        stocks = client.pages('erp.goodsstocks.list', {
            'ptypeids': [int(product_id)], 'ktypeids': [int(BUSINESS_WAREHOUSE_ID)]},
            'goodsstocks', lambda row: tuple(_text(row.get(key)) for key in
                ('profileid', 'ktypeid', 'ptypeid', 'skuid')), size=100, limit=2)
        if any(_text(row.get('profileid')) != plan['profile_id'] or
                _text(row.get('ktypeid')) != BUSINESS_WAREHOUSE_ID or
                _text(row.get('ptypeid')) != product_id or
                _text(row.get('skuid')) != sku_id for row in stocks) or len(stocks) > 1:
            return _outcome('unknown', 'new_product_created_stock_identity_conflict')
        stock = stocks[0] if stocks else {'qty': '0', 'saleqty': '0'}
        quantities = {}
        for key in ('qty', 'saleqty'):
            value = Decimal(_text(stock.get(key)))
            if not value.is_finite():
                return _outcome('unknown', 'new_product_created_stock_quantity_invalid')
            quantities[key] = format(value.normalize(), 'f')
        inventory = {**ident, 'id': stable_id('stock', ident), 'sku': plan['code'],
            'product_name': product['product_name'], 'spec': product['spec'],
            'model': product['model'], 'material': product['material'],
            'standard_no': plan['source']['standard'], 'grade': plan['source']['grade'],
            'surface': plan['source']['surface'], 'location': '', 'warehouse': '江苏优螺',
            'stock_quantity': quantities['qty'], 'available_stock': quantities['saleqty'],
            'unit_name': CONFIRMED_BASE_UNIT, 'unit_evidence': unit_evidence([CONFIRMED_BASE_UNIT]),
            'status': '', 'print_blocked': False, 'has_image': False,
            'new_product_identity_verified': True,
        }
        return _outcome('created', '', created_code=plan['code'], product_id=product_id,
            sku_id=sku_id, verified_inventory_row=inventory,
            verification='exact_code_ptype_and_sku_unit', verified_product_sha256=_hash(raw))
    except Exception:
        return _outcome('unknown', 'new_product_verification_unavailable')


def execute_confirmed_product_creation(client, plan, *, confirmed_plan_hash, pre_submit_check=None):
    """One frozen add, followed by exact verification. Caller first persists creating."""
    try:
        validate_confirmed_plan(plan, confirmed_plan_hash)
        if _text(client.account()) != plan['profile_id']:
            return _outcome('blocked', 'new_product_live_profile_conflict')
        context = fetch_new_product_context(client, plan['profile_id'])
        classes = {row['classification_path']: row for row in classification_choices(context['classes'], plan['profile_id'])}
        category = classes.get(plan['classification_path'])
        if not category or category['type_id'] != plan['payload']['ParTypeId']:
            return _outcome('blocked', 'new_product_live_classification_changed')
        if any(_text(row.get('usercode')).upper() == plan['code'] for row in context['products']) or _lookup(client, plan['code']):
            return _outcome('blocked', 'new_product_code_already_exists')
        expected = plan['payload']
        if any(product['active'] and
                _key(product['product_name']) == _key(expected['FullName']) and
                _size(product['spec']) == _size(expected['Standard']) and
                _key(product['model']) == _key(expected.get('Type')) and
                _key(product['material']) == _key(expected.get('Area'))
                for product in map(_product, context['products'])):
            return _outcome('blocked', 'new_product_identity_already_exists')
    except ValueError as error:
        safe = str(error) if str(error).startswith('new_product_') else 'new_product_preflight_unavailable'
        return _outcome('blocked', safe)
    except Exception:
        return _outcome('blocked', 'new_product_preflight_unavailable')
    if pre_submit_check is None:
        return _outcome('blocked', 'new_product_submission_authority_missing')
    try:
        if pre_submit_check() is not True:
            return _outcome('blocked', 'new_product_submission_authority_changed')
    except Exception:
        return _outcome('blocked', 'new_product_submission_authority_changed')
    # Once create_product is called, every exception is an unknown write. The
    # durable queue must retain it and offer verify-only recovery, never retry.
    try:
        response = client.create_product(plan, confirmed_plan_hash)
    except Exception:
        return _outcome('unknown', 'new_product_write_result_unknown')
    if not isinstance(response, dict):
        return _outcome('unknown', 'new_product_write_response_invalid')
    created, errors = response.get('ptypes'), response.get('errorinfos', [])
    if (not isinstance(created, list) or not isinstance(errors, list) or errors or
            len(created) != 1 or not isinstance(created[0], dict) or
            _text(created[0].get('usercode')).upper() != plan['code'] or
            not _text(created[0].get('id') or created[0].get('ptypeid')) or
            not _text(created[0].get('skuid'))):
        return _outcome('unknown', 'new_product_write_not_single_confirmed_identity')
    return verify_created_product(client, plan, receipt=created[0])
