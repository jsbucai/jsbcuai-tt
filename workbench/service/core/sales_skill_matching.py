"""Bounded, read-only matching context for the N150 sales Codex worker.

The frozen NAS rules are candidate evidence, never inventory mutations or
permission to submit. Read the current API catalog once per batch, preserve
source rows, and return a small candidate set without historical prices.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from .sales_history_matching import history_customer_binding_current, search_candidates
from .unit_policy import CONFIRMED_BASE_UNIT, unit_evidence


MAX_LINES = 500
MAX_CATALOG = 30000
MAX_HISTORY_SCAN = 2000
RULE_FILES = (
    '商品名称同义词.json', '表面处理同义词.json', '标准关系规则.json',
    '允许替代关系.json', '客户料号规则.json', '商品解析规则.json',
)
STANDARD_RE = re.compile(
    r'(?<![A-Z0-9])(?:GB/T|GBT|JB/T|JB/ZQ|HG/T|GB|DIN|ISO|EN|ASME|ASTM|JIS|BS|NF|UNI)'
    r'\s*[-/]?\s*[0-9]+(?:\.[0-9]+)*(?:\([A-Z0-9]+\)|[A-Z])?(?:-[0-9]{4})?(?![A-Z0-9])', re.I)
SIZE_RE = re.compile(r'(?<![A-Z0-9])M\s*\d+(?:\.\d+)?(?:\s*[X×*]\s*\d+(?:\.\d+)?){0,3}', re.I)
GRADE_RE = re.compile(
    r'(?<![A-Z0-9.])(?:A[24]\s*-\s*\d{2}|12\.9|10\.9|8\.8|5\.8|4\.8|45H|(?:100|140|200|300)HV)(?![A-Z0-9.])', re.I)


def _text(value):
    return '' if value is None else str(value).strip()


def _known(value):
    value = _text(value)
    return '' if value in ('-', '—', '/') else value


def _key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', _known(value))).upper()


def _yes(value):
    return value is True or (not isinstance(value, bool) and _text(value).lower() in ('1', '是', 'true'))


@dataclass(frozen=True)
class SalesSkillRules:
    documents: dict
    hashes: dict
    source: str


def load_skill_rules(directory):
    """Load six server-owned JSON files bound to a checked-in digest manifest.

    No path comes from an App request. No Python or code in the NAS package is
    executed. Invalid or modified rules fail closed rather than falling back.
    """
    path = Path(directory)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('sales_skill_rules_directory_invalid')
    manifest_path = path / 'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('sales_skill_rules_symlink_rejected')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('sales_skill_rules_manifest_invalid') from None
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or
            not isinstance(manifest.get('files'), dict) or set(manifest['files']) != set(RULE_FILES)):
        raise ValueError('sales_skill_rules_manifest_invalid')
    documents = {}
    for name in RULE_FILES:
        file = path / name
        expected = manifest['files'][name]
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('sales_skill_rules_hash_invalid')
        if file.is_symlink() or not file.is_file() or file.stat().st_size > 128 * 1024:
            raise ValueError('sales_skill_rules_file_invalid')
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('sales_skill_rules_hash_mismatch')
        try:
            document = json.loads(raw)
        except ValueError:
            raise ValueError('sales_skill_rules_json_invalid') from None
        if not isinstance(document, dict):
            raise ValueError('sales_skill_rules_json_invalid')
        documents[name] = document
    return SalesSkillRules(documents, dict(manifest['files']), _text(manifest.get('source')))


def _standards(text, rules):
    values = {_key(match.group()).replace('(', '').replace(')', '') for match in STANDARD_RE.finditer(text)}
    if rules:
        for rule in rules.documents['标准关系规则.json'].get('normalization_rules', []):
            # DIN127 without B is an explicitly documented variant risk; its
            # recall alias is not accepted as proof of the B variant.
            if rule.get('type') != 'same_standard_number':
                continue
            aliases = {_key(item) for item in rule.get('aliases', [])}
            canonical = _key(rule.get('canonical'))
            values = {canonical if value in aliases else value for value in values}
    return sorted(values)


def _family(name, rules):
    stripped = STANDARD_RE.sub('', _text(name)).strip()
    if not rules:
        return _key(stripped), []
    document = rules.documents['商品名称同义词.json']
    prefixes = sorted(document.get('prefixes_to_strip_before_family_match', []), key=len, reverse=True)
    for prefix in prefixes:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
            break
    value = _key(stripped)
    matches = []
    for row in document.get('canonical_rules', []):
        for alias in row.get('synonyms', []):
            normalized = _key(alias)
            if normalized and (value == normalized or normalized in value):
                matches.append((len(normalized), _key(row.get('canonical')), _text(alias)))
    if not matches:
        return value, []
    _, canonical, alias = max(matches, key=lambda item: (item[0], item[1]))
    generic = {_key(item.get('value')) for item in document.get('ambiguous_names', [])}
    return canonical, ['ambiguous_product_name'] if _key(alias) in generic else []


def _grade(value):
    value = _key(value).removesuffix('级')
    try:
        return format(Decimal(value).normalize(), 'f')
    except InvalidOperation:
        return value


def _size(value):
    value = unicodedata.normalize('NFKC', _known(value)).upper()
    stripped = STANDARD_RE.sub(' ', value)
    match = SIZE_RE.search(stripped)
    if match:
        size = _key(match.group()).replace('×', 'X').replace('*', 'X')
        return 'M' + 'X'.join(format(Decimal(part).normalize(), 'f') for part in size[1:].split('X'))
    compact = _key(stripped).replace('×', 'X').replace('*', 'X')
    if re.fullmatch(r'\d+(?:\.\d+)?(?:X\d+(?:\.\d+)?){0,3}', compact):
        return 'X'.join(format(Decimal(part).normalize(), 'f') for part in compact.split('X'))
    return compact


def _surface(value, rules):
    raw = _key(value)
    if not rules:
        return raw, []
    document = rules.documents['表面处理同义词.json']
    for row in document.get('canonical_rules', []):
        if raw in {_key(item) for item in row.get('synonyms', [])}:
            canonical = _key(row.get('canonical'))
            # Preserve process/availability exceptions instead of silently
            # dropping them when comparing a coating family.
            risks = []
            if raw in {_key('研磨+发黑'), _key('发黑+研磨')}:
                risks.append('additional_grinding_process')
            if raw in {_key('特殊热浸锌'), _key('现货即可'), _key('现货颜色')}:
                risks.append('surface_special_or_ambiguous')
            return canonical, risks
    return raw, []


def _derive_attributes(raw, rules):
    family, risks = _family(raw.get('name'), rules)
    grade = _grade(raw.get('grade'))
    model = _text(raw.get('model'))
    all_text = ' '.join(_text(raw.get(field)) for field in ('name', 'spec', 'model', 'standard', 'material'))
    if not grade:
        matches = {_grade(match.group()) for match in GRADE_RE.finditer(all_text)}
        if len(matches) == 1:
            grade = matches.pop()
        elif len(matches) > 1:
            risks.append('multiple_grade_tokens')
        else:
            match = re.search(r'(?<!\d)(4|5|6|8|10|12)\s*级(?!\d)', all_text)
            if match:
                grade = _grade(match.group(1))
    surface, surface_risks = _surface(raw.get('surface'), rules)
    if not surface and rules:
        matches = []
        compact = _key(model)
        for rule in rules.documents['表面处理同义词.json'].get('canonical_rules', []):
            for alias in rule.get('synonyms', []):
                needle = _key(alias)
                if needle and needle in compact:
                    matches.append((len(needle), alias))
        if matches:
            _, alias = max(matches, key=lambda item: item[0])
            surface, surface_risks = _surface(alias, rules)
    spec = _size(raw.get('spec'))
    # In the current catalogue small round nuts use M85-2 for diameter/pitch.
    # Scope this spelling rule to that family; a bolt's dash may mean length.
    raw_spec = _key(raw.get('spec'))
    if family == _key('小圆螺母') and re.fullmatch(r'M\d+(?:\.\d+)?-\d+(?:\.\d+)?', raw_spec):
        spec = _size(raw_spec.replace('-', 'X'))
    if (re.fullmatch(r'\d+(?:\.\d+)?(?:X\d+(?:\.\d+)?){0,3}', spec) and
            any(token in family for token in ('垫圈', '螺栓', '螺钉', '螺母', '螺柱'))):
        spec = 'M' + spec
    return {'family': family, 'spec': spec, 'grade': grade,
            'surface': surface, 'material': _key(raw.get('material')),
            'standards': _standards(all_text, rules), 'risks': sorted(set(risks + surface_risks))}


def catalog_attributes(product):
    """Derive catalog fields from explicit API standard_no/model tokens.

    No rule equivalence or default grade/coating is applied. Conflicting or
    missing model tokens remain empty. Provenance participates in the caller's
    version-bound source review; the original inventory payload is unchanged.
    """
    if not isinstance(product, dict):
        raise ValueError('sales_catalog_product_invalid')
    output = {key: _text(product.get(key)) for key in
              ('product_name', 'spec', 'model', 'grade', 'surface', 'material')}
    output['standard'] = _text(product.get('standard_no') or product.get('standard'))
    provenance = {key: {'field': key, 'raw': output[key]} for key in output if output[key]}
    if output['standard']:
        provenance['standard']['field'] = 'standard_no' if _text(product.get('standard_no')) else 'standard'
    risks = []
    model = unicodedata.normalize('NFKC', output['model'])
    if not output['grade']:
        matches = {_key(match.group()) for match in GRADE_RE.finditer(model)}
        if not matches:
            matches = {match.group(1) for match in re.finditer(r'(?<!\d)(4|5|6|8|10|12)\s*级(?!\d)', model)}
        if len(matches) == 1:
            output['grade'] = matches.pop()
            provenance['grade'] = {'field': 'model', 'raw': output['model'], 'method': 'unique_explicit_grade_token'}
        elif len(matches) > 1:
            risks.append('multiple_grade_tokens')
    if not output['surface']:
        # Literal recognized finish tokens, not NAS synonym transformations.
        tokens = ('特殊热浸锌', '研磨+发黑', '发黑+研磨', '热镀锌', '热浸锌', '复合涂层',
                  '达克罗', '蓝白锌', '兰白锌', '镀蓝白锌', '镀兰锌', '发黑',
                  '镀锌', '白锌', '本色', '镀镍', '镀铬')
        matches = {token for token in tokens if token in model}
        matches = {token for token in matches if not any(token != other and token in other for other in matches)}
        if len(matches) == 1:
            output['surface'] = matches.pop()
            provenance['surface'] = {'field': 'model', 'raw': output['model'], 'method': 'unique_explicit_finish_token'}
        elif len(matches) > 1:
            risks.append('multiple_surface_tokens')
    output['attribute_provenance'] = provenance
    output['attribute_review_reasons'] = risks
    return output


def _source(line, rules):
    reference = (line.get('source_ref') if isinstance(line.get('source_ref'), dict) else
                 line.get('_ref') if isinstance(line.get('_ref'), dict) else line)
    raw = {field: _known(reference.get('original_' + field)) for field in
           ('name', 'spec', 'grade', 'surface', 'material', 'standard')}
    return reference, raw, _derive_attributes(raw, rules)


def _cached_inactive_products(db, profile_id):
    """Use an integrity-checked full/merged catalog's explicit inactive facts.

    Older inventory projections omitted deleted flags. This is an additional
    negative check, never positive proof that a missing cache row is active.
    A damaged, partial, other-profile or future cache cannot be used.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='erp_reference_cache'").fetchone():
        return set()
    row = db.execute("SELECT profile,payload,fetched_at,source_hash FROM erp_reference_cache WHERE kind='catalog-v1'").fetchone()
    if row is None or _text(row[0]) != profile_id:
        return set()
    try:
        fetched = datetime.fromisoformat(row[2])
        if fetched.tzinfo is None or fetched > datetime.now(timezone.utc):
            return set()
        payload = json.loads(row[1])
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if hashlib.sha256(encoded.encode()).hexdigest() != row[3]:
            return set()
        products = payload.get('products')
        if (not isinstance(products, list) or len(products) > MAX_CATALOG or
                any(not isinstance(product, dict) or not _text(product.get('ptypeid')) for product in products)):
            return set()
        ids = [_text(product['ptypeid']) for product in products]
        if len(ids) != len(set(ids)):
            return set()
        if any(_text(product.get('profileid')) not in ('', profile_id) for product in products):
            return set()
        return {_text(product['ptypeid']) for product in products
            if _yes(product.get('isstop')) or _yes(product.get('deleted'))}
    except (ValueError, TypeError, KeyError):
        return set()


def _catalog_index(db, profile_id, rules):
    grouped = defaultdict(list)
    inactive_products = _cached_inactive_products(db, profile_id)
    rows = db.execute("SELECT id,sku,payload FROM records WHERE kind='inventory' ORDER BY sku,id LIMIT ?",
                      (MAX_CATALOG + 1,)).fetchall()
    complete = len(rows) <= MAX_CATALOG
    for record_id, code, payload in rows[:MAX_CATALOG]:
        try:
            product = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if (not isinstance(product, dict) or _text(product.get('profile_id')) != profile_id or
                _text(product.get('warehouse_name') or product.get('warehouse')) != '江苏优螺'):
            continue
        identity = {key: _text(product.get(key)) for key in
                    ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')}
        names = {product.get(key) for key in ('unit_name', 'base_unit_name', 'unit')}
        evidence = unit_evidence(names) if identity['base_unit'] == '1' else None
        reasons = []
        if not _text(code) or not all(identity.values()):
            reasons.append('current_product_identity_incomplete')
        if (any(_yes(product.get(field)) for field in ('is_stopped', 'is_deleted', 'isstop', 'deleted')) or
                identity['product_id'] in inactive_products or
                _text(product.get('status')).lower() in ('stopped', 'deleted', 'disabled', '停用', '已停用', '删除', '已删除')):
            reasons.append('current_product_inactive')
        if not evidence:
            reasons.append('current_unit_unverified')
        fields = catalog_attributes(product)
        raw = {'name': fields['product_name'], **{key: fields[key] for key in
               ('spec', 'grade', 'surface', 'material', 'model', 'standard')}}
        reasons += fields['attribute_review_reasons']
        attrs = _derive_attributes(raw, rules)
        grouped[_text(code)].append({'product_code': _text(code), 'record_id': record_id,
            'current_identity': identity, 'product_name': raw['name'], 'spec': raw['spec'],
            'model': raw['model'], 'material': raw['material'], 'unit': CONFIRMED_BASE_UNIT if evidence else '',
            'current_attributes': attrs, 'attribute_sources': raw, 'review_reasons': reasons})
        grouped[_text(code)][-1]['attribute_provenance'] = fields['attribute_provenance']
    by_size_family = defaultdict(list)
    unique = {}
    for code, products in grouped.items():
        if len(products) != 1:
            # Never pick one record from conflicting current identities.
            continue
        item = products[0]
        unique[code] = item
        attrs = item['current_attributes']
        if attrs['family'] and attrs['spec']:
            by_size_family[(attrs['family'], attrs['spec'])].append(item)
    return unique, by_size_family, complete, len(rows)


def _allowed_grade(source, current, rules):
    if not rules or source['family'] != _key('平垫圈') or current['family'] != source['family']:
        return None
    # A missing material is not sufficient proof of carbon steel in the
    # current API catalog. The old rule cannot broaden a stainless product.
    carbon = {_key(value) for value in ('碳钢', 'Q235', 'Q235A', 'Q215')}
    if source['material'] not in carbon or current['material'] not in carbon:
        return None
    for rule in rules.documents['允许替代关系.json'].get('rules', []):
        if rule.get('status') != '启用' or rule.get('attribute') != '等级':
            continue
        allowed = set(_standards(' '.join(rule.get('applicable_standards', [])), rules))
        if (rule.get('material_scope') == 'carbon_steel_only' and
                _grade(rule.get('customer_value')) == source['grade'] and
                _grade(rule.get('match_as')) == current['grade'] and
                source['standards'] and set(source['standards']).issubset(allowed)):
            return _text(rule.get('rule_id'))
    return None


def _standard_relation(source, current, rules):
    if set(source['standards']).issubset(current['standards']):
        return []
    if not rules:
        return None
    for rule in rules.documents['标准关系规则.json'].get('explicit_equivalences', []):
        if (rule.get('status') != '启用' or rule.get('relation') != 'erp_match_equivalent' or
                _key(rule.get('scope')) != source['family'] or current['family'] != source['family']):
            continue
        standards = set(_standards(' '.join(rule.get('standards', [])), rules))
        if (source['standards'] and current['standards'] and
                set(source['standards']).issubset(standards) and set(current['standards']) & standards):
            return ['nas_explicit_standard_relation:' + '|'.join(sorted(standards))]
    return None


def _compare(source, item, rules):
    current = item['current_attributes']
    reasons = list(item['review_reasons']) + source['risks'] + current['risks']
    applied = []
    for field in ('family', 'spec', 'grade', 'surface', 'material'):
        if not source[field]:
            continue
        if not current[field]:
            reasons.append('catalog_' + field + '_missing')
        elif source[field] != current[field]:
            allowed = _allowed_grade(source, current, rules) if field == 'grade' else None
            if allowed:
                applied.append('nas_allowed_grade:' + allowed)
            else:
                reasons.append('catalog_' + field + '_conflict')
    if source['standards']:
        if not current['standards']:
            reasons.append('catalog_standard_missing')
        else:
            relation = _standard_relation(source, current, rules)
            if relation is None:
                reasons.append('catalog_standard_conflict')
            else:
                applied += relation
    return sorted(set(reasons)), sorted(set(applied))


def _part_history(db, reference_db, profile, customer, reference, coverage, catalog, source, rules, limit):
    """Recheck pinned same-customer or explicitly allowed group part evidence.

    This uses the existing verified projection, not the unvalidated raw NAS
    index. Group sharing is allowed only by the original DANIELI rule. No
    frequency threshold or general cross-customer fallback is used.
    """
    part = _known(reference.get('customer_part_no'))
    if not part or not coverage.get('available') or coverage.get('customer_identity_verified') is False:
        return None
    digest = coverage.get('source_sha256')
    metadata_row = reference_db.execute('SELECT metadata FROM sales_matching_sources WHERE profile_id=? '
        'AND source_sha256=? AND active=1', (profile, digest)).fetchone()
    private_metadata = json.loads(metadata_row[0]) if metadata_row else {}
    clauses = ['profile_id=?', 'source_sha256=?', 'customer_name=?', 'customer_part_no=?']
    params = [profile, digest, customer, part]
    where = ' AND '.join(clauses)
    count = reference_db.execute('SELECT count(*) FROM sales_matching_history WHERE ' + where, params).fetchone()[0]
    method = 'customer_part_exact'
    if count == 0 and rules:
        for group in rules.documents['客户料号规则.json'].get('customer_groups', []):
            if group.get('part_number_scope') != 'group_global':
                continue
            pattern_match = any(isinstance(rule.get('raw_pattern'), str) and
                re.fullmatch(rule['raw_pattern'], part, flags=re.I)
                for rule in group.get('match_part_number_patterns', []))
            customer_match = any(_key(needle) in _key(customer) for needle in
                                 group.get('match_customer_name_contains', []) if _key(needle))
            if pattern_match or customer_match:
                clauses = ['profile_id=?', 'source_sha256=?', 'customer_part_no=?']
                params = [profile, digest, part]
                # A matching part pattern itself grants exactly this group
                # scope. A group name without a pattern shares only records
                # whose customer belongs to that declared group.
                if not pattern_match:
                    names = [_key(needle) for needle in group.get('match_customer_name_contains', []) if _key(needle)]
                    # Values are bound data, not SQL text; SQLite instr avoids
                    # LIKE wildcard characters widening a customer scope.
                    clauses.append('(' + ' OR '.join('instr(upper(customer_name),?)>0' for _ in names) + ')')
                    params += names
                where = ' AND '.join(clauses)
                count = reference_db.execute('SELECT count(*) FROM sales_matching_history WHERE ' + where, params).fetchone()[0]
                method = 'nas_declared_group_part'
                break
    if count > MAX_HISTORY_SCAN:
        return {'candidates': [], 'review_reasons': ['history_candidate_scope_too_large'], 'matched_order_lines': count}
    if count == 0:
        # Preserve same-customer attribute fallback when no part mapping exists.
        return None
    grouped = defaultdict(list)
    for row in reference_db.execute('''SELECT erp_code,source_row_id,order_date,source_file,source_excel_row,
        raw_summary,attributes,eligible,review_reasons,customer_name FROM sales_matching_history WHERE ''' + where +
        ' ORDER BY order_date DESC,source_row_id', params):
        grouped[row[0]].append(row)
    output = []
    group_ambiguous = method == 'nas_declared_group_part' and len(grouped) != 1
    for code, rows in grouped.items():
        item = catalog.get(code)
        if not item:
            continue
        current_reasons, applied = _compare(source, item, rules)
        reliable = []
        uncertain = []
        for row in rows:
            stored = json.loads(row[6]).get('raw', {})
            old_attrs = _derive_attributes(stored, rules)
            old_reasons, old_applied = _compare(old_attrs, item, rules)
            # Compare requested attributes with the historical original, in
            # addition to the current API identity.
            pseudo = {'current_attributes': old_attrs, 'review_reasons': []}
            original_reasons, original_applied = _compare(source, pseudo, rules)
            row_reasons = sorted(set(json.loads(row[8]) + old_reasons + original_reasons + current_reasons))
            identity_review = history_customer_binding_current(db, profile, row[9], private_metadata)
            if identity_review:
                row_reasons = sorted(set(row_reasons + [identity_review]))
            if row[7] and not row_reasons:
                reliable.append(row)
                applied += old_applied + original_applied
            else:
                uncertain.append((row, row_reasons or ['history_row_ineligible']))
        reasons = list(current_reasons)
        if not reliable:
            reasons += sorted({reason for _, values in uncertain for reason in values})
        if group_ambiguous:
            reasons.append('nas_group_part_multiple_erp_codes')
        evidence_rows = reliable or [row for row, _ in uncertain]
        proof = [{'source_sha256': digest, 'source_row_id': row[1], 'order_date': row[2],
            'source_file': row[3], 'source_excel_row': row[4], 'raw_summary': row[5],
            'historical_attributes': json.loads(row[6]).get('raw', {})}
            for row in evidence_rows[:3]]
        output.append({**item, 'method': method, 'source_evidence': proof,
            'evidence_count': len(reliable), 'history_line_count': len(rows),
            'history_review_line_count': len(uncertain), 'last_order_date': max(row[2] for row in rows),
            'applied_rules': sorted(set(applied)), 'review_reasons': sorted(set(reasons)),
            'available': bool(reliable) and not reasons, 'requires_confirmation': True})
    output.sort(key=lambda item: (not item['available'], -item['evidence_count'], item['product_code']))
    return {'candidates': output[:limit], 'candidate_count': len(output), 'matched_order_lines': count,
            'review_reasons': ['nas_group_part_multiple_erp_codes'] if group_ambiguous else []}


def build_matching_context(db, profile_id, customer_name, source_lines, history_db=None, limit=5, *, rules=None):
    """Return small candidate sets; no prices, writes, network or file opening.

    Caller supplies the authenticated session scope and server-owned source
    lines. The model may propose a candidate, but the service must revalidate
    identity/source/draft versions when applying it. All candidates require
    final draft confirmation. Current catalog is scanned once for the batch.
    """
    if (not isinstance(profile_id, str) or not profile_id.strip() or
            not isinstance(customer_name, str) or not customer_name.strip() or
            not isinstance(source_lines, list) or len(source_lines) > MAX_LINES or
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5):
        raise ValueError('sales_skill_matching_scope_invalid')
    ids = []
    for line in source_lines:
        if not isinstance(line, dict):
            raise ValueError('sales_skill_source_line_invalid')
        reference = (line.get('source_ref') if isinstance(line.get('source_ref'), dict) else
                     line.get('_ref') if isinstance(line.get('_ref'), dict) else line)
        identity = _text(reference.get('id') or line.get('source_line_id') or line.get('line_id'))
        if not identity or identity in ids:
            raise ValueError('sales_skill_source_line_identity_invalid')
        ids.append(identity)
    catalog, catalog_index, complete, scanned = _catalog_index(db, profile_id, rules)
    lines = []
    history_coverage = {'available': False, 'complete_current_history': False}
    for identity, line in zip(ids, source_lines):
        reference, raw, source = _source(line, rules)
        historical = search_candidates(db, profile_id, customer_name, reference,
                                       limit=limit, history_db=history_db)
        history_coverage = historical['coverage']
        exact_part_history = _part_history(db, history_db if history_db is not None else db,
            profile_id, customer_name, reference, history_coverage, catalog, source, rules, limit)
        if exact_part_history is not None:
            historical = {**historical, **exact_part_history}
        candidates = {}
        for old in historical['candidates']:
            item = catalog.get(old['product_code'])
            if not item:
                continue
            reasons, applied = _compare(source, item, rules)
            # Historical source integrity/attribute conflicts remain visible;
            # current attrs were independently derived from the API model.
            previous_reasons = (old.get('review_reasons', []) if exact_part_history is not None else
                                [reason for reason in old.get('review_reasons', []) if not reason.startswith('catalog_')])
            reasons = sorted(set(reasons + previous_reasons))
            candidates[item['product_code']] = {**item, 'method': old['method'],
                'source_evidence': old['source_evidence'], 'evidence_count': old['evidence_count'],
                'last_order_date': old['last_order_date'], 'applied_rules': applied,
                'review_reasons': reasons, 'available': complete and not reasons,
                'requires_confirmation': True}
        explicit_code = _text(line.get('product_code'))
        proposed = [catalog[explicit_code]] if explicit_code in catalog else []
        if source['family'] and source['spec']:
            proposed += catalog_index.get((source['family'], source['spec']), [])
        for item in proposed:
            if item['product_code'] in candidates:
                continue
            reasons, applied = _compare(source, item, rules)
            if item['product_code'] != explicit_code and any(reason.endswith('_conflict') for reason in reasons):
                continue
            candidates[item['product_code']] = {**item,
                'method': 'explicit_product_code' if item['product_code'] == explicit_code else 'current_catalog_attributes',
                'source_evidence': [], 'evidence_count': 0, 'last_order_date': '', 'applied_rules': applied,
                'review_reasons': reasons, 'available': complete and not reasons,
                'requires_confirmation': True}
        values = list(candidates.values())
        values.sort(key=lambda item: (not item['available'],
            item['method'] not in ('customer_part_exact', 'explicit_product_code'),
            -item['evidence_count'], item['product_code']))
        available = [item for item in values if item['available']]
        preferred = [item for item in available if item['method'] in
                     ('customer_part_exact', 'explicit_product_code')]
        # A reliable, unique customer-part mapping supplies the missing finish;
        # generic catalogue variants are alternatives, not conflicting history.
        # Two reliable mappings remain ambiguous regardless of frequency.
        chosen = preferred[0] if len(preferred) == 1 else (
            available[0] if not preferred and len(available) == 1 else None)
        missing = [field for field in ('family', 'spec') if not source[field]]
        lines.append({'source_line_id': identity, 'source_attributes': raw,
            'derived_source_attributes': source, 'candidates': values[:limit],
            'candidate_count': len(values), 'available_candidate_count': len(available),
            'suggested_product_code': chosen['product_code'] if chosen and not missing else '',
            'selection_basis': chosen['method'] if chosen and not missing else '',
            'requires_confirmation': True, 'needs_question': bool(missing) or chosen is None,
            'missing_attributes': missing, 'history_status': historical['status'],
            'history_review_reasons': historical.get('review_reasons', []),
            'automatic_submission': False})
    return {'schema_version': 1, 'profile_id': profile_id, 'customer_name': customer_name,
            'catalog_complete': complete, 'catalog_records_scanned': scanned,
            'history_coverage': history_coverage,
            'rules_source': rules.source if rules else '', 'rules_sha256': rules.hashes if rules else {},
            'history_prices_used': False, 'automatic_submission': False, 'lines': lines}
