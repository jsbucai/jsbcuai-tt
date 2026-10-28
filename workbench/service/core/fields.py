import re
from .rules import text,parse_note,quantity,number_text

def order_total(item):
    row_total = quantity(item.get('quantity'))
    return row_total if row_total > 0 else 0

def order_fields(item, package=None):
    parsed = {key: value for key, value in parse_note(item.get('system_note') or item.get('summary')).items()
              if text(value) not in ('', '-')}
    if item.get('delivery_date_confirmation_id') and item.get('delivery_date'):
        parsed['deliveryDate'] = text(item['delivery_date'])
    total = order_total(item)
    package = package or {}
    fields = {'orderNo': text(item.get('order_no')), 'shippingId': text(item.get('shipping_id')), 'buyer': text(item.get('buyer')), 'productName': text(item.get('product_name')), 'name': text(item.get('product_name')), 'spec': text(item.get('spec')), 'grade': text(item.get('grade')), 'surface': text(item.get('surface')), 'systemNote': text(item.get('system_note')), 'status': text(item.get('status')), **parsed}
    # Shared summaries describe the customer label; SKU and quantity always
    # describe this actual order line, even for repeated SKUs in one order.
    fields.update(sku=text(item.get('sku')), quantity=number_text(total),
                  erpOrderNo=text(item.get('order_no')), lineId=text(item.get('line_id')))
    qr_value = text(item.get('order_no'))
    for key in ('shipping_id', 'source_shipping_id'):
        candidate = text(item.get(key))
        match = re.fullmatch(r'@\d{2}/\d{2}#(.+)', candidate)
        if ((match and match.group(1) == text(item.get('order_no'))) or
                (candidate and not match and (key == 'source_shipping_id' or not item.get('profile_id')))):
            qr_value = candidate
            break
    fields.update(totalPackages=number_text(package.get('totalPackages') or 1), packageIndex=number_text(package.get('packageIndex') or 1), packageQuantity=number_text(package.get('packageQuantity') if package.get('packageQuantity') is not None else total), qrValue=qr_value)
    return fields

def inventory_fields(item):
    mapping = {'productName': 'product_name', 'sku': 'sku', 'spec': 'spec', 'model': 'model', 'material': 'material', 'location': 'location', 'productNote': 'product_note', 'standardNo': 'standard_no', 'quantity': 'stock_quantity', 'availableStock': 'available_stock'}
    fields = {key: text(item.get(value) or item.get(key)) for (key, value) in mapping.items()}
    fields['sku'] = text(item.get('sku')).upper()
    fields['qrValue'] = text(item.get('qrValue')) or fields['sku']
    return fields
