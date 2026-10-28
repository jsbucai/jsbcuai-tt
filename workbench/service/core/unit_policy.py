"""User-confirmed display unit for ERP base-unit ID 1.

The ERP labels are retained as source evidence. This policy says nothing about
another ERP unit ID, a WMS conversion rate, or a platform purchase pack.
"""

CONFIRMED_BASE_UNIT = '支'
USER_UNIT_EVIDENCE = 'user_confirmed_base_unit_1_zhi_2026_10_07'
USER_OVERRIDE_UNIT_EVIDENCE = 'user_confirmed_all_base_unit_1_zhi_2026_10_07'
ERP_UNIT_EVIDENCE = 'erp_named_base_unit_1'
VALID_UNIT_EVIDENCE = frozenset({
    USER_UNIT_EVIDENCE, USER_OVERRIDE_UNIT_EVIDENCE, ERP_UNIT_EVIDENCE,
})
WEB_BLANK_UNIT_BASIS = 'user_blank_default_piece'
WEB_EXPLICIT_UNIT_BASIS = 'erp_explicit_piece'
WEB_MIXED_UNIT_BASIS = 'mixed_user_blank_and_erp_explicit_piece'


def unit_evidence(raw_names):
    """Return provenance for unit ID 1, or None for an unknown numeric ID.

    Raw names such as 个, PCS, 箱 and 件 are display labels only. The owner's
    explicit instruction maps them to 支, while the caller keeps the raw text.
    """
    names = {str(value).strip() for value in raw_names if value is not None and str(value).strip()}
    if any(name.isdecimal() and name != '1' for name in names):
        return None
    labels = names - {'1'}
    if any(name != CONFIRMED_BASE_UNIT for name in labels):
        return USER_OVERRIDE_UNIT_EVIDENCE
    return ERP_UNIT_EVIDENCE if labels else USER_UNIT_EVIDENCE


def web_readback_piece(raw_unit, base_unit_id):
    """Normalize only a reviewed base-unit-1 sales line's ERP display unit.

    The owner's explicit blank-unit rule covers both an empty ERP cell and an
    absent independent unit column. It never converts another base unit ID or
    a nonempty ERP display label other than 支.
    """
    if str(base_unit_id).strip() != '1':
        raise ValueError('web_readback_base_unit_not_one')
    value = '' if raw_unit is None else str(raw_unit).strip()
    if not value:
        return CONFIRMED_BASE_UNIT, WEB_BLANK_UNIT_BASIS
    if value == CONFIRMED_BASE_UNIT:
        return CONFIRMED_BASE_UNIT, WEB_EXPLICIT_UNIT_BASIS
    raise ValueError('web_readback_unit_conflict')


def web_readback_piece_basis(raw_unit, source, base_unit_id, other_unit=None):
    """Require explicit readback provenance before applying blank-unit policy."""
    raw = '' if raw_unit is None else str(raw_unit).strip()
    alias = '' if other_unit is None else str(other_unit).strip()
    if any(value not in ('', CONFIRMED_BASE_UNIT) for value in (raw, alias)) or (
            raw and alias and raw != alias):
        raise ValueError('unit_conflict')
    if not base_unit_id:
        return ''
    if str(base_unit_id).strip() != '1':
        raise ValueError('base_unit_conflict')
    if source not in ('column_absent', 'cell'):
        raise ValueError('unit_readback_unavailable')
    if source == 'column_absent' and (raw or alias):
        raise ValueError('unit_conflict')
    try:
        return web_readback_piece(raw, base_unit_id)[1]
    except ValueError:
        raise ValueError('unit_conflict') from None
