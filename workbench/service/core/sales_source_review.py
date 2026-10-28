"""Conservative comparison of a sales source row with its chosen ERP product.

An explicit product code selects a candidate; it is not proof that the source
description is compatible. Confirmation records returned by this module are
server-owned facts. Never restore them from an untrusted request body.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import re
from collections.abc import Mapping


SCHEMA = 'sales_source_match_v1'
IDENTITY_FIELDS = ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')
SOURCE_FIELDS = ('id', 'kind', 'source_id', 'sha256', 'sheet', 'row', 'preview',
                 'original_name', 'original_spec', 'original_grade', 'original_surface',
                 'customer_part_no', 'customer_order_no', 'project_no', 'customer_line_no',
                 'original_standard')
CATALOG_FIELDS = ('product_name', 'spec', 'model', 'grade', 'surface', 'standard')
_DIMENSION = re.compile(r'M(\d+(?:\.\d+)?)[xX*×](\d+(?:\.\d+)?)\Z', re.IGNORECASE)
_GRADE = re.compile(r'(\d+(?:\.\d+)?)(?:级)?\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_STANDARD = re.compile(r'(?<![A-Z0-9])(?:GB/T|JB/T|HG/T|GB|DIN|ISO|EN|ASME|ASTM|JIS|BS|NF|UNI)\s*[-/]?\s*[0-9]+(?:\.[0-9]+)*(?:-[0-9]{4})?(?![A-Z0-9])', re.I)


def _text(value):
    return '' if value is None else str(value)


def _comparison_text(value):
    return re.sub(r'\s+', '', _text(value)).casefold()


def _size(value):
    """Normalize only a complete M diameter by length size, never standards."""
    match = _DIMENSION.fullmatch(re.sub(r'\s+', '', _text(value)))
    if not match:
        return None
    return tuple(Decimal(part) for part in match.groups())


def _grade(value):
    match = _GRADE.fullmatch(re.sub(r'\s+', '', _text(value)))
    return Decimal(match.group(1)) if match else None


def inspect_source_match(source_ref: Mapping | None, catalog: Mapping,
                         resolved_identity: Mapping) -> dict:
    """Return a revision-bound review, retaining raw source and catalog facts.

    Missing source fields make no compatibility claim. Different names may
    be an abbreviation, but still require a deliberate human decision. A
    grade or finish is compared only with its actual catalog field, not a
    guessed substring from the model. Every catalog fact, including model,
    participates in the revision, so later changes invalidate confirmation.
    """
    if not isinstance(catalog, Mapping) or not isinstance(resolved_identity, Mapping):
        raise ValueError('source review requires catalog and resolved identity')
    if source_ref is not None and not isinstance(source_ref, Mapping):
        raise ValueError('source reference must be an object')
    source_ref = source_ref or {}
    source = {key: _text(source_ref.get(key)) for key in SOURCE_FIELDS}
    actual = {key: _text(catalog.get(key)) for key in CATALOG_FIELDS}
    if _text(source_ref.get('original_material')).strip():
        source['original_material']=_text(source_ref['original_material'])
        actual['material']=_text(catalog.get('material'))
    identity = {key: _text(resolved_identity.get(key)) for key in IDENTITY_FIELDS}
    binding = {'schema': SCHEMA, 'source': source, 'catalog': actual, 'identity': identity}
    revision = sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':')).encode()).hexdigest()
    checks, reasons = [], []
    fields = (('name', 'original_name', 'product_name', '品名'),
              ('spec', 'original_spec', 'spec', '规格'),
              ('grade', 'original_grade', 'grade', '等级'),
              ('surface', 'original_surface', 'surface', '表面处理'))
    if 'original_material' in source:
        fields=(*fields,('material','original_material','material','材质／ERP资料Area'))
    for field, source_key, catalog_key, label in fields:
        given, expected = source[source_key], actual[catalog_key]
        left, right = _comparison_text(given), _comparison_text(expected)
        reason = ''
        if not left:
            status = 'not_provided'
        elif not right:
            status = 'catalog_missing'
            reason = f'来源有{label}，商品资料该字段为空，请核对原件与所选商品'
        elif left == right:
            status = 'matched'
        elif field == 'spec' and _size(given) is not None and _size(given) == _size(expected):
            status = 'matched_size_symbols'
        elif field == 'grade' and _grade(given) is not None and _grade(given) == _grade(expected):
            status = 'matched_grade_suffix'
        elif field == 'name' and min(len(left), len(right)) >= 2 and (left in right or right in left):
            status = 'possible_abbreviation'
            reason = '来源品名可能为简称，请确认原件与所选商品属于同一商品'
        else:
            status = 'differs'
            reason = f'来源{label}与商品资料不同，请核对原件与所选商品'
        checks.append({'field': field, 'source': given, 'catalog': expected,
                       'status': status, 'reason': reason})
        if reason:
            reasons.append(reason)
    if source['original_standard'].strip():
        # Compare only an explicit standard identifier. Never declare GB,
        # DIN or ISO aliases equivalent or infer a standard from the size.
        given=source['original_standard']
        expected=' '.join(value for value in (actual['standard'],actual['model'],actual['spec']) if value)
        left={_comparison_text(match.group()) for match in _STANDARD.finditer(given)}
        right={_comparison_text(match.group()) for match in _STANDARD.finditer(expected)}
        matched=bool(left and left==right) or bool(actual['standard'] and
            _comparison_text(given)==_comparison_text(actual['standard']))
        reason='' if matched else f'来源标准“{given}”与商品资料尚未证明一致，请核对原件与所选商品'
        checks.append({'field':'standard','source':given,'catalog':expected,
                       'status':'matched' if matched else 'differs' if right else 'catalog_missing',
                       'reason':reason})
        if reason:
            reasons.append(reason)
    return {'schema': SCHEMA, 'revision': revision, 'requires_confirmation': bool(reasons),
            'source': source, 'catalog': actual, 'identity': identity,
            'checks': checks, 'reasons': reasons}


def make_source_confirmation(review: Mapping, actor_id: str, recorded_at: str) -> dict:
    """Build a confirmation using the authenticated server actor and clock."""
    if (not isinstance(review, Mapping) or review.get('schema') != SCHEMA or
            not isinstance(review.get('revision'), str) or not _HASH.fullmatch(review['revision'])):
        raise ValueError('source review evidence is invalid')
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError('source confirmation requires an authenticated actor')
    try:
        moment = datetime.fromisoformat(recorded_at)
        if moment.tzinfo is None:
            raise ValueError('timezone missing')
    except (TypeError, ValueError):
        raise ValueError('source confirmation requires a server timestamp with timezone') from None
    return {'schema': SCHEMA, 'revision': review['revision'],
            'confirmed_by': actor_id, 'confirmed_at': recorded_at}


def source_confirmation_valid(record: Mapping | None, review: Mapping) -> bool:
    """Check a persisted server-owned fact against the current complete revision."""
    if (not isinstance(record, Mapping) or not isinstance(review, Mapping) or
            review.get('schema') != SCHEMA or record.get('schema') != SCHEMA or
            not isinstance(review.get('revision'), str) or not _HASH.fullmatch(review['revision']) or
            record.get('revision') != review['revision'] or
            not isinstance(record.get('confirmed_by'), str) or not record['confirmed_by'].strip()):
        return False
    try:
        return datetime.fromisoformat(record['confirmed_at']).tzinfo is not None
    except (KeyError, TypeError, ValueError):
        return False
