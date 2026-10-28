"""Arrival notes for a purchase line. These stay on the local worksheet.

A customer entry defaults to 「简称需求数量」, for example 海速需求120.
A rework demand is exactly 「改制」 so receiving can find the rework sheet
without guessing a quantity. Callers may replace the text before saving.
"""
from decimal import Decimal

from .purchase_workspace_data import decimal, number, text

REWORK = '改制'
REWORK_CARD = '改制'
_SUFFIXES = ('股份有限公司', '有限责任公司', '有限公司', '集团公司', '公司')


def customer_short_name(name):
    """Drop a trailing company suffix. Names longer than six characters keep the first four."""
    raw = text(name)
    for suffix in _SUFFIXES:
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[:-len(suffix)].strip()
            break
    if not raw:
        raw = text(name)
    if len(raw) > 6:
        raw = raw[:4]
    return raw


def _blob(*values):
    return ''.join(text(value) for value in values)


def demand_is_rework(demand):
    if not isinstance(demand, dict):
        return False
    if text(demand.get('rework')) in {'1', 'true', REWORK}:
        return True
    return REWORK in _blob(demand.get('summary'), demand.get('note'), demand.get('product_name'),
                           demand.get('process'))


def demand_on_rework_card(demand, item):
    """Rework lines, and any line of a rework product, belong on the 改制 card."""
    if REWORK in _blob(item.get('product_name'), item.get('spec'), item.get('model')):
        return True
    return demand_is_rework(demand)


def card_name_for_demand(demand, item):
    if demand_on_rework_card(demand, item):
        return REWORK_CARD
    return text(demand.get('customer_name'))


def item_is_rework(item, customer=''):
    """The 改制 card and a rework product always use the rework note.

    A normal customer card does not: its rework lines are listed on 改制 instead.
    """
    if not isinstance(item, dict):
        return False
    if text(customer) == REWORK_CARD:
        return True
    if REWORK in _blob(item.get('product_name'), item.get('spec'), item.get('model')):
        return True
    if text(customer):
        return False
    demands = [row for row in item.get('demands') or [] if isinstance(row, dict)]
    return bool(demands) and all(demand_on_rework_card(row, item) for row in demands)


def note_quantity(item, customer, line_quantity):
    """Customer entry uses that customer's open demand; otherwise the line quantity."""
    customer = text(customer)
    if customer and customer != REWORK_CARD:
        total = sum((decimal(row.get('quantity') or 0) for row in item.get('demands') or []
                     if isinstance(row, dict) and text(row.get('customer_name')) == customer
                     and not demand_on_rework_card(row, item)), Decimal(0))
        if total > 0:
            return total
    if line_quantity not in (None, ''):
        return decimal(line_quantity)
    return Decimal(0)


def default_purchase_note(item, customer='', line_quantity=None):
    customer = text(customer)
    if customer == REWORK_CARD or item_is_rework(item, customer):
        return REWORK
    name = customer
    if not name:
        names = []
        for row in item.get('demands') or []:
            if not isinstance(row, dict):
                continue
            value = text(row.get('customer_name'))
            if demand_on_rework_card(row, item):
                continue
            if value and value not in names:
                names.append(value)
        if len(names) == 1:
            name = names[0]
    short = customer_short_name(name)
    if not short:
        return ''
    quantity = note_quantity(item, name, line_quantity)
    if quantity <= 0:
        return short + '需求'
    return short + '需求' + number(quantity)
