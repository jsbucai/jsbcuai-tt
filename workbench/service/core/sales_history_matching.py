"""Versioned historical evidence for sales candidates, never automatic orders.

Only this module's additive sales_matching_* tables are written by the explicit
importer. search_candidates is read-only, uses one exact customer and current
inventory identities, and never supplies prices or changes a sales draft.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from .unit_policy import CONFIRMED_BASE_UNIT, unit_evidence


SCHEMA_VERSION = 1
WAREHOUSE_NAME = '江苏优螺'
IDENTITY = ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')
MAX_SCAN = 2000
_STANDARD = re.compile(r'(?<![A-Z0-9])(?:GB/T|JB/T|HG/T|GB|DIN|ISO|EN|ASME|ASTM|JIS|BS|NF|UNI)\s*[-/]?\s*[0-9]+(?:\.[0-9]+)*(?:-[0-9]{4})?(?![A-Z0-9])', re.I)
_GRADE = re.compile(r'(\d+(?:\.\d+)?)\s*级?\Z')
_SIZE = re.compile(r'M\d+(?:\.\d+)?(?:[X×*]\d+(?:\.\d+)?)*\Z', re.I)


def _text(value):
    return '' if value is None else str(value).strip()


def _known(value):
    value = _text(value)
    return '' if value in ('-', '—', '/') else value


def _yes(value):
    return value is True or (not isinstance(value, bool) and _text(value).lower() in ('1', '是', 'true'))


def _key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', _known(value))).casefold()


def _standards(value):
    return sorted({_key(match.group()).upper() for match in _STANDARD.finditer(
        unicodedata.normalize('NFKC', _known(value)).upper())})


def _attribute(value, field):
    raw = _STANDARD.sub('', unicodedata.normalize('NFKC', _known(value))) if field in ('name', 'spec') else _known(value)
    compact = _key(raw)
    if field == 'grade':
        match = _GRADE.fullmatch(compact)
        if match:
            return format(Decimal(match.group(1)).normalize(), 'f')
    if field == 'spec' and _SIZE.fullmatch(compact):
        parts = re.split(r'[x×*]', compact[1:], flags=re.I)
        return 'm' + 'x'.join(format(Decimal(part).normalize(), 'f') for part in parts)
    return compact


def _attributes(name='', spec='', grade='', surface='', material='', standard=''):
    raw = {'name': _known(name), 'spec': _known(spec), 'grade': _known(grade),
           'surface': _known(surface), 'material': _known(material)}
    return {'raw': raw, **{field: _attribute(value, field) for field, value in raw.items()},
            'standards': _standards(' '.join((raw['name'], raw['spec'], _known(standard))))}


def _source_attributes(source):
    return _attributes(source.get('original_name'), source.get('original_spec'),
                       source.get('original_grade'), source.get('original_surface'),
                       source.get('original_material'), source.get('original_standard'))


def _decimal(value):
    raw = _known(value)
    if ',' in raw:
        if not re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', raw):
            return None
        raw = raw.replace(',', '')
    try:
        result = Decimal(raw)
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError):
        return None


def _date(value):
    value = _text(value)[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ''


def _validate_history(row):
    """Check parsed index facts against the original right-hand fields.

    The first field remains the PO; any extra # can belong only to the
    project field and must equal its stored parsed value. No general guessing.
    """
    reasons = []
    if not _yes(row.get('is_usable_for_history')):
        reasons.append('history_marked_unusable')
    if not _yes(row.get('remark_system_code_matches_erp')):
        reasons.append('history_system_code_conflict')
    if _yes(row.get('summary_review_required')) or row.get('summary_source') == 'conflict':
        reasons.append('history_summary_conflict')
    raw = _text(row.get('raw_remark'))
    without_delivery = raw
    if re.search(r'(?:^|#)\s*交期\s*[=:：]', raw):
        match = re.search(r'#\s*交期\s*[=:：]\s*(\d{4}-\d{2}-\d{2})\s*$', raw)
        if not match or not _date(match.group(1)):
            reasons.append('history_delivery_suffix_invalid')
        else:
            without_delivery = raw[:match.start()]
    parts = [_known(part) for part in without_delivery.split('#')]
    if len(parts) < 10:
        reasons.append('history_summary_unparsed')
    else:
        tail = parts[-8:]
        fields = ('order_line', 'customer_part_no', 'customer_product_name',
                  'customer_standard_spec', 'customer_grade', 'customer_surface')
        for field, given in zip(fields, tail[:6]):
            if _known(row.get(field)) != given:
                reasons.append('history_parsed_field_conflict:' + field)
        if _known(row.get('customer_order_no')) != parts[0]:
            reasons.append('history_parsed_field_conflict:customer_order_no')
        if _known(row.get('project_no')) != '#'.join(parts[1:-8]):
            reasons.append('history_parsed_field_conflict:project_no')
        if tail[-1] != _known(row.get('erp_code')):
            reasons.append('history_raw_sku_conflict')
        quantity = _decimal(tail[-2])
        indexed = _decimal(row.get('customer_quantity_text'))
        if indexed is None:
            indexed = _decimal(row.get('customer_quantity_num'))
        if quantity is None or indexed is None or quantity != indexed:
            reasons.append('history_raw_quantity_conflict')
    if not _known(row.get('customer_name')) or not _known(row.get('erp_code')):
        reasons.append('history_identity_missing')
    if not _date(row.get('order_date')):
        reasons.append('history_order_date_invalid')
    return sorted(set(reasons))


def ensure_matching_schema(db):
    """Explicit import only; does not change Store migrations or existing tables."""
    statements = (
        '''CREATE TABLE IF NOT EXISTS sales_matching_sources(
            profile_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_name TEXT NOT NULL,
            source_bytes INTEGER NOT NULL, schema_version INTEGER NOT NULL, imported_at TEXT NOT NULL,
            coverage_start TEXT NOT NULL, coverage_end TEXT NOT NULL, metadata TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(profile_id,source_sha256))''',
        '''CREATE TABLE IF NOT EXISTS sales_matching_history(
            profile_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_row_id TEXT NOT NULL,
            customer_name TEXT NOT NULL, customer_code TEXT NOT NULL, customer_part_no TEXT NOT NULL,
            erp_code TEXT NOT NULL, order_date TEXT NOT NULL, source_file TEXT NOT NULL,
            source_excel_row TEXT NOT NULL, raw_summary TEXT NOT NULL, attributes TEXT NOT NULL,
            name_key TEXT NOT NULL, spec_key TEXT NOT NULL, grade_key TEXT NOT NULL,
            surface_key TEXT NOT NULL, eligible INTEGER NOT NULL, review_reasons TEXT NOT NULL,
            PRIMARY KEY(profile_id,source_sha256,source_row_id))''',
        '''CREATE INDEX IF NOT EXISTS sales_matching_part
            ON sales_matching_history(profile_id,source_sha256,customer_name,customer_part_no)''',
        '''CREATE INDEX IF NOT EXISTS sales_matching_attributes
            ON sales_matching_history(profile_id,source_sha256,customer_name,name_key,spec_key)''',
    )
    for statement in statements:
        db.execute(statement)


def file_sha256(path):
    value = sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def import_history_snapshot(db, source_path, profile_id, expected_sha256):
    """Import a verified frozen index atomically into additive tables only.

    Caller owns commit/rollback. The CLI permits only its isolated destination;
    this function accepts an explicit connection for supervised integration.
    """
    path = Path(source_path)
    if path.is_symlink() or not path.is_file() or not _text(profile_id):
        raise ValueError('history_source_or_profile_invalid')
    main_path = next((row[2] for row in db.execute('PRAGMA database_list') if row[1]=='main'), '')
    if main_path and Path(main_path).resolve() == path.resolve():
        raise ValueError('history_source_target_same')
    if not re.fullmatch(r'[0-9a-f]{64}', _text(expected_sha256)) or file_sha256(path) != expected_sha256:
        raise ValueError('history_source_hash_mismatch')
    source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    source.row_factory = sqlite3.Row
    source.execute('PRAGMA query_only=ON')
    try:
        columns = {row[1] for row in source.execute('PRAGMA table_info(historical_orders)')}
        required = {'order_row_id','customer_name','customer_code','erp_code','order_date','source_file',
            'source_excel_row','raw_remark','customer_order_no','project_no','order_line',
            'customer_part_no','customer_product_name','customer_standard_spec','customer_grade',
            'customer_surface','customer_quantity_text','customer_quantity_num',
            'is_usable_for_history','remark_system_code_matches_erp'}
        if not required.issubset(columns):
            raise ValueError('history_source_schema_incomplete')
        ensure_matching_schema(db)
        existing = db.execute('SELECT metadata FROM sales_matching_sources WHERE profile_id=? AND source_sha256=?',
                              (profile_id, expected_sha256)).fetchone()
        if existing:
            return {'status':'already_imported', **json.loads(existing[0])}
        db.execute('SAVEPOINT sales_matching_import')
        try:
            total = eligible = 0
            reasons_count = defaultdict(int)
            periods = set(); dates = []
            for stored in source.execute('SELECT * FROM historical_orders ORDER BY order_row_id'):
                row = dict(stored)
                identity = _text(row.get('order_row_id'))
                if not identity:
                    raise ValueError('history_source_row_identity_missing')
                reasons = _validate_history(row)
                attrs = _attributes(row.get('customer_product_name'),row.get('customer_standard_spec'),
                                    row.get('customer_grade'),row.get('customer_surface'))
                order_date = _date(row.get('order_date'))
                if order_date: dates.append(order_date)
                if row.get('data_period'): periods.add(_text(row['data_period']))
                values = (profile_id, expected_sha256, identity, _text(row.get('customer_name')),
                    _text(row.get('customer_code')), _known(row.get('customer_part_no')), _text(row.get('erp_code')),
                    order_date, _text(row.get('source_file')), _text(row.get('source_excel_row')),
                    '' if row.get('raw_remark') is None else str(row['raw_remark']),
                    json.dumps(attrs,ensure_ascii=False), attrs['name'], attrs['spec'],
                    attrs['grade'], attrs['surface'], int(not reasons),json.dumps(reasons,ensure_ascii=False))
                db.execute('INSERT INTO sales_matching_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
                total += 1; eligible += int(not reasons)
                for reason in reasons: reasons_count[reason] += 1
            if total == 0 or not dates:
                raise ValueError('history_source_empty_or_undated')
            metadata = {'source_sha256':expected_sha256, 'imported_rows':total,'eligible_rows':eligible,
                'review_rows':total-eligible,'review_reason_counts':dict(sorted(reasons_count.items())),
                'declared_periods':sorted(periods),'coverage_start':min(dates),'coverage_end':max(dates),
                'complete_current_history':False,'prices_imported':False}
            db.execute('UPDATE sales_matching_sources SET active=0 WHERE profile_id=?', (profile_id,))
            db.execute('INSERT INTO sales_matching_sources VALUES (?,?,?,?,?,?,?,?,?,1)',
                (profile_id,expected_sha256,path.name,path.stat().st_size,SCHEMA_VERSION,
                 datetime.now(timezone.utc).isoformat(timespec='seconds'),min(dates),max(dates),
                 json.dumps(metadata,ensure_ascii=False)))
            db.execute('RELEASE sales_matching_import')
            return {'status':'imported',**metadata}
        except Exception:
            db.execute('ROLLBACK TO sales_matching_import'); db.execute('RELEASE sales_matching_import')
            raise
    finally:
        source.close()


def _comparison(given, actual, *, missing_is_review=True):
    reasons = []
    for field in ('name','spec','grade','surface','material'):
        if given.get(field):
            if not actual.get(field):
                if missing_is_review: reasons.append('catalog_' + field + '_missing')
            elif given[field] != actual[field]:
                reasons.append('catalog_' + field + '_conflict')
    if given.get('standards'):
        if not actual.get('standards'):
            if missing_is_review: reasons.append('catalog_standard_missing')
        elif not set(given['standards']).issubset(actual['standards']):
            reasons.append('catalog_standard_conflict')
    return reasons


def _catalog(db, profile, code):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
        return None, ['current_catalog_unavailable']
    products = []
    for record_id, payload in db.execute("SELECT id,payload FROM records WHERE kind='inventory' AND sku=?", (code,)):
        try: product = json.loads(payload)
        except (ValueError, TypeError): continue
        if not isinstance(product, dict) or _text(product.get('profile_id')) != profile:
            continue
        if _text(product.get('warehouse_name') or product.get('warehouse')) != WAREHOUSE_NAME:
            continue
        products.append((record_id,product))
    if len(products) != 1:
        return None, ['current_product_missing' if not products else 'current_product_identity_ambiguous']
    record_id, product = products[0]
    identity = {field:_text(product.get(field)) for field in IDENTITY}
    reasons = []
    if not all(identity.values()): reasons.append('current_product_identity_incomplete')
    if _yes(product.get('is_stopped')) or _yes(product.get('is_deleted')):
        reasons.append('current_product_inactive')
    names = {product.get(field) for field in ('unit_name','base_unit_name','unit')}
    evidence = unit_evidence(names) if identity['base_unit'] == '1' else None
    if not evidence: reasons.append('current_unit_unverified')
    attrs = _attributes(product.get('product_name'),product.get('spec'),product.get('grade'),
                        product.get('surface'),product.get('material'),
                        ' '.join((_text(product.get('standard')), _text(product.get('model')))))
    return {'record_id':record_id,'identity':identity,'product_name':_text(product.get('product_name')),
            'model':_text(product.get('model')),
            'attributes':attrs,'unit':CONFIRMED_BASE_UNIT if evidence else '', 'unit_evidence':evidence}, reasons


def history_customer_binding_current(db, profile_id, customer_name, metadata):
    """Validate a prepared reference's private identity proof against current policy.

    Older isolated references without proof metadata remain compatible. A
    formally prepared reference never guesses an ID after a binding changes.
    """
    proofs = metadata.get('customer_binding_proofs')
    if proofs is None:
        return ''
    if not isinstance(proofs, dict):
        return 'history_customer_binding_proof_invalid'
    proof = proofs.get(sha256(customer_name.encode()).hexdigest())
    if not isinstance(proof, dict) or proof.get('profile_id') != profile_id:
        return 'history_current_customer_unverified'
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        return 'history_current_customer_policy_missing'
    row = db.execute("SELECT value FROM settings WHERE key='sales_web_import_policy'").fetchone()
    try:
        policy = json.loads(row[0]) if row else {}
        binding = policy.get('customer_bindings', {}).get(customer_name, {})
        customer_id = _text(binding.get('erp_customer_id'))
        valid = (policy.get('profile_id') == profile_id and binding.get('profile_id') == profile_id
                 and binding.get('customer_name') == customer_name and customer_id
                 and binding.get('account_id') == proof.get('account_id') == policy.get('account_id')
                 and sha256(customer_id.encode()).hexdigest() == proof.get('erp_customer_id_sha256'))
    except (ValueError, TypeError, AttributeError):
        valid = False
    return '' if valid else 'history_current_customer_identity_changed'


def search_candidates(db, profile_id, customer_name, source_ref, limit=5, *, history_db=None):
    """Read exact-customer evidence and recheck each code against current data.

    source_ref supports customer_part_no and original_name/spec/grade/surface/
    material/standard. It must be server-owned. Caller enforces session access.
    All returned candidates still require a deliberate human choice. An
    explicitly supplied history_db may hold the isolated reference tables;
    current inventory is always read from db. This function never opens paths,
    attaches databases, or changes either connection.
    """
    if not _text(profile_id) or not _text(customer_name) or not isinstance(source_ref,dict):
        raise ValueError('history_search_scope_invalid')
    limit = min(5,max(1,int(limit)))
    reference = history_db if history_db is not None else db
    tables = {row[0] for row in reference.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    empty = {'status':'not_prepared','coverage':{'available':False,'complete_current_history':False},
             'candidates':[],'review_reasons':[],'matched_order_lines':0,'automatic_selection':False,
             'message':'历史参考资料尚未准备','covered_from':'','covered_to':''}
    if not {'sales_matching_sources','sales_matching_history'}.issubset(tables):
        return empty
    sources = reference.execute('''SELECT source_sha256,metadata,imported_at,schema_version FROM sales_matching_sources
        WHERE profile_id=? AND active=1''', (profile_id,)).fetchall()
    if len(sources) != 1 or sources[0][3] != SCHEMA_VERSION:
        return {**empty,'message':'历史资料版本或来源不唯一，需管理员重新准备',
                'review_reasons':['history_source_not_unique_or_schema_changed']}
    digest, metadata, imported_at, _ = sources[0]
    # Binding proofs are private service metadata, not all-customer context
    # returned to an employee or a model handling one customer's order.
    private_metadata = json.loads(metadata)
    public_metadata = {key: value for key, value in private_metadata.items()
                       if key != 'customer_binding_proofs'}
    coverage = {**public_metadata,'available':True,'imported_at':imported_at,
                'status':'historical_reference','source_sha256':digest,'complete_current_history':False}
    base = {**empty,'status':'ready','coverage':coverage,
            'message':'只读同客户历史候选；需人工核对，不采用历史价格',
            'covered_from':coverage.get('coverage_start',''),'covered_to':coverage.get('coverage_end','')}
    binding_review = history_customer_binding_current(db, profile_id, customer_name, private_metadata)
    if binding_review:
        return {**base, 'status': 'needs_review', 'review_reasons': [binding_review],
                'message': '当前客户身份与历史绑定不一致，请先由管理员核对',
                'coverage': {**coverage, 'customer_identity_verified': False}}
    if 'customer_binding_proofs' in private_metadata:
        base['coverage'] = {**coverage, 'customer_identity_verified': True}
    if _yes(source_ref.get('summary_review_required')) or source_ref.get('summary_source') == 'conflict':
        return {**base,'status':'needs_review','message':'来源摘要存在冲突，请先核对原文件',
                'review_reasons':['source_summary_conflict']}
    attrs = _source_attributes(source_ref)
    part = _known(source_ref.get('customer_part_no'))
    clauses = ['profile_id=?','source_sha256=?','customer_name=?']; params = [profile_id,digest,customer_name]
    tier = 'same_customer_part'
    if part:
        clauses.append('customer_part_no=?');params.append(part)
    else:
        tier = 'same_customer_attributes'
        if not attrs['name'] or not attrs['spec']:
            return {**base,'status':'needs_review','message':'请补充客户料号，或完整品名和规格后查询候选',
                    'review_reasons':['source_part_or_exact_attributes_required']}
        clauses.extend(('name_key=?','spec_key=?'));params.extend((attrs['name'],attrs['spec']))
        for field in ('grade','surface'):
            if attrs[field]:clauses.append(field+'_key=?');params.append(attrs[field])
    where = ' AND '.join(clauses)
    count = reference.execute('SELECT count(*) FROM sales_matching_history WHERE '+where,params).fetchone()[0]
    part_not_found = bool(part and count == 0)
    if part_not_found and attrs['name'] and attrs['spec']:
        # Missing part mappings may fall back only to this same exact customer
        # and complete provided name/size. Never fall back across customers.
        tier = 'same_customer_attributes'
        clauses = ['profile_id=?','source_sha256=?','customer_name=?','name_key=?','spec_key=?']
        params = [profile_id,digest,customer_name,attrs['name'],attrs['spec']]
        for field in ('grade','surface'):
            if attrs[field]:clauses.append(field+'_key=?');params.append(attrs[field])
        where=' AND '.join(clauses)
        count=reference.execute('SELECT count(*) FROM sales_matching_history WHERE '+where,params).fetchone()[0]
    if count > MAX_SCAN:
        return {**base,'status':'needs_review','message':'同客户历史范围过大，请补充规格后再核对',
                'review_reasons':['history_candidate_scope_too_large'],
                'matched_order_lines':count}
    grouped = defaultdict(list)
    fields = 'erp_code,source_row_id,order_date,source_file,source_excel_row,raw_summary,attributes,eligible,review_reasons'
    for row in reference.execute('SELECT '+fields+' FROM sales_matching_history WHERE '+where+
                          ' ORDER BY order_date DESC,source_row_id',params):
        stored_attrs=json.loads(row[6])
        # A supplied explicit standard must match the historical original too.
        if tier == 'same_customer_attributes' and attrs['standards'] and attrs['standards'] != stored_attrs['standards']:
            continue
        grouped[row[0]].append(row)
    candidates=[]
    for code, history in grouped.items():
        current, reasons = _catalog(db,profile_id,code)
        reliable=[]; uncertain=[]
        for row in history:
            row_reasons = json.loads(row[8])
            old_attrs=json.loads(row[6])
            # Conflicts between this request and history are not overwritten.
            row_reasons += ['source_history_'+r.removeprefix('catalog_') for r in _comparison(attrs,old_attrs)]
            if current:
                row_reasons += _comparison(old_attrs,current['attributes'])
                row_reasons += _comparison(attrs,current['attributes'])
            if row[7] and not row_reasons and not reasons: reliable.append(row)
            else: uncertain.append((row,sorted(set(row_reasons+reasons))))
        selected = reliable or [item[0] for item in uncertain]
        proof=[]
        for row in selected[:3]:
            proof.append({'source_sha256':digest,'source_row_id':row[1],'order_date':row[2],
                'source_file':row[3],'source_excel_row':row[4],'raw_summary':row[5],
                'historical_attributes':json.loads(row[6])['raw']})
        all_reasons=sorted({reason for _,row_reasons in uncertain for reason in row_reasons})
        if len(grouped)>1: all_reasons.append('same_customer_multiple_erp_codes')
        candidates.append({'product_code':code,'match_tier':tier,'usable':bool(reliable),
            'product_name':current['product_name'] if current else json.loads(history[0][6])['raw']['name'],
            'spec':current['attributes']['raw']['spec'] if current else '',
            'model':current.get('model','') if current else '',
            'method':'customer_part_exact' if tier=='same_customer_part' else 'same_customer_history_attributes',
            'evidence_count':len(reliable),
            'last_order_date':max(row[2] for row in history),'available':bool(reliable),
            'message':'有同客户核对依据，选择后仍须确认草稿' if reliable else '历史或当前商品仍需核对，不能直接采用',
            'requires_confirmation':True,'evidence_status':'verified' if reliable else 'needs_review',
            'history_line_count':len(history),'reliable_line_count':len(reliable),
            'review_reasons':all_reasons,'source_evidence':proof,
            'current_product':current,'current_identity':current['identity'] if current else None})
    candidates.sort(key=lambda item:(not item['usable'],-item['reliable_line_count'],item['product_code']))
    return {**base,'status':'ready' if candidates else 'no_candidates','candidates':candidates[:limit],
            'message':('同一客户料号存在多个ERP编码，请按原件人工选择' if len(grouped)>1 else
                       '有同客户历史候选，请核对原件及当前商品资料' if candidates else
                       '当前客户范围内未找到符合条件的历史候选'),
            'review_reasons':(['same_customer_multiple_erp_codes'] if len(grouped)>1 else [])+
                             (['exact_customer_part_not_found'] if part_not_found else []),
            'candidate_count':len(candidates),'matched_order_lines':count}
