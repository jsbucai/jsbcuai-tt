"""Deterministic customer quote conversion for reviewed sales drafts.

The customer-facing quote remains separate from the ERP-facing untaxed price.
No tax amount or ERP header total is inferred here: those require the accepted
ERP write contract and a readback of the exact document.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext


VAT_PERCENT = "13"
VAT_FACTOR = Decimal("1.13")
UNIT_PRECISION = Decimal("0.001")
AMOUNT_PRECISION = Decimal("0.01")


def quote_basis(value):
    """Return the explicit quote basis, defaulting only a missing value."""
    if value is None or str(value).strip() == "":
        return "tax_inclusive"
    basis = str(value).strip().lower()
    if basis in {"tax_inclusive", "含税", "含税价"}:
        return "tax_inclusive"
    if basis in {"tax_exclusive", "未税", "不含税", "未税价", "不含税价"}:
        return "tax_exclusive"
    raise ValueError("税价口径须明确为含税或未税")


def is_fixed_vat_rate(value):
    """An omitted rate inherits 13%; an explicit value must equal 13%."""
    if value is None or str(value).strip() == "":
        return True
    raw = str(value).strip().removesuffix("%").strip()
    try:
        rate = Decimal(raw)
    except (ValueError,ArithmeticError):
        return False
    return rate.is_finite() and rate == Decimal(VAT_PERCENT)


def quote_price(quantity, quoted_unit_price, basis):
    """Return (untaxed unit price, untaxed line amount) as fixed-scale text.

    First round the converted unit price to three decimals, then multiply by
    quantity and round the line amount to two decimals. This order is a
    business rule; the ERP adapter must verify its own price/total semantics.
    """
    if basis not in {"tax_inclusive", "tax_exclusive"}:
        raise ValueError("税价口径无效")
    with localcontext() as context:
        context.prec = 64
        quantity = Decimal(str(quantity))
        quote = Decimal(str(quoted_unit_price))
        if not quantity.is_finite() or quantity <= 0 or not quote.is_finite() or quote < 0:
            raise ValueError("数量或报价无效")
        untaxed = quote / VAT_FACTOR if basis == "tax_inclusive" else quote
        unit = untaxed.quantize(UNIT_PRECISION, rounding=ROUND_HALF_UP)
        amount = (quantity * unit).quantize(AMOUNT_PRECISION, rounding=ROUND_HALF_UP)
        if unit.adjusted() > 15 or amount.adjusted() > 15:
            raise ValueError("报价金额超出范围")
        return format(unit, ".3f"), format(amount, ".2f")
