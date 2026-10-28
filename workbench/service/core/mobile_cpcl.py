"""Verified 5088 mobile positions; no raster, hardware, or prior app imports."""
from .rules import text, PRINT_ALL_CODE, PRINT_ALL_PACK_CODE
WIDTH, HEIGHT = 400, 800

def clean(value):
    return text(value) or '-'

def header():
    return [f'! 0 200 200 {HEIGHT} 1', f'PAGE-WIDTH {WIDTH}', 'GAP-SENSE']

def point(x, y, rotate=0):
    return (int(y), HEIGHT - int(x)) if rotate == 90 else (int(x), int(y))

def native_text(value, x, y, bold=1, mag_x=1, mag_y=1, rotate=0):
    (px, py) = point(x, y, rotate)
    command = 'VTEXT' if rotate == 90 else 'TEXT'
    return '\r\n'.join([f'! U1 SETBOLD {bold}', f'SETMAG {mag_x} {mag_y}', f'{command} 55 0 {px} {py} {clean(value)}', 'SETMAG 1 1', '! U1 SETBOLD 0'])

def qr(value, x, y, unit=4, rotate=0):
    (px, py) = point(x, y, rotate)
    command = 'VB QR' if rotate == 90 else 'B QR'
    return '\r\n'.join([f'{command} {px} {py} M 2 U {unit}', f'MA,{clean(value)}', 'ENDQR'])

def finish(commands):
    return '\r\n'.join([*commands, 'FORM', 'PRINT', '']).encode('gb18030', errors='replace')

def order_cpcl(fields):
    styles = {'label': (0, 1, 1), 'miniBold': (2, 1, 1), 'important': (3, 2, 2), 'title': (4, 2, 2), 'emphasis': (4, 2, 2)}

    def label(value, x, y, style='important'):
        (bold, mx, my) = styles[style]
        return native_text(value, x, y, bold, mx, my, 90)
    commands = header() + ['BOX 8 8 390 790 1', label('NAME:', 76, 30, 'label'), label(fields.get('name'), 196, 22, 'title'), label('PO.NO:', 66, 90, 'label'), label(fields.get('orderNo'), 196, 86), label('JOB.NO:', 58, 136, 'label'), label(fields.get('projectNo'), 196, 132), label('TP:', 102, 178, 'label'), label(fields.get('spec'), 196, 174), label('CT:', 102, 224, 'label'), label(fields.get('grade'), 196, 220), label('QTY:', 90, 286, 'label'), label(fields.get('quantity'), 196, 282), label('CTNS:', 78, 338, 'label'), label('共', 196, 334), label(fields.get('totalPackages') or 1, 264, 328, 'emphasis'), label('件', 318, 334), label('本件', 378, 334), label(fields.get('packageQuantity'), 452, 328, 'emphasis'), label('PCS', 582, 334), label('LINE.NO:', 532, 90, 'label'), label(fields.get('lineNo'), 660, 86), label('ID:', 474, 136, 'label'), label(fields.get('drawingNo'), 540, 132), label('0', 650, 178, 'miniBold'), label('kg', 724, 178, 'miniBold'), label(fields.get('surface'), 442, 224), label('PCS', 442, 286), qr(fields.get('qrValue'), 650, 204, rotate=90), label(fields.get('sku'), 650, 334)]
    return finish(commands)

def visual_width(value):
    return sum((1 if ord(char) < 128 else 2 for char in str(value or '')))

def wrap(value, max_width, max_lines=2):
    value = clean(value)
    (lines, current, width) = ([], '', 0)
    for char in value:
        char_width = 1 if ord(char) < 128 else 2
        if current and width + char_width > max_width:
            lines.append(current)
            (current, width) = (char, char_width)
            if len(lines) >= max_lines:
                break
        else:
            current += char
            width += char_width
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        lines = ['-']
    if visual_width(value) > max_width * max_lines:
        last = lines[-1]
        while last and visual_width(last + '...') > max_width:
            last = last[:-1]
        lines[-1] = (last or '-') + '...'
    return lines

def inventory_cpcl(fields):

    def value(key, width):
        return ' '.join(wrap(fields.get(key), width, 1))

    def label(value, x, y, bold=1, mag_x=1, mag_y=1):
        return native_text(value, x, y, bold, mag_x, mag_y, 90)
    sku = fields.get('sku') or fields.get('qrValue') or '-'
    commands = header() + [label(value('productName', 38), 18, 20, 5, 2, 2), label(value('spec', 12), 146, 86, 5, 4, 4), label(value('model', 18), 48, 194, 5, 3, 3)]
    if text(fields.get('material')):
        commands.append(label(value('material', 8), 222, 276, 4, 2, 2))
    commands += [label('货位:', 72, 344, 3), label(value('location', 10), 200, 344, 4), qr(fields.get('qrValue') or sku, 592, 122, unit=5, rotate=90), label(sku, 590, 326, 5, 2, 2)]
    if text(fields.get('standardNo')):
        commands.append(label(fields['standardNo'], 20, 62))
    return finish(commands)

def function_codes_cpcl():
    return finish(header() + ['BOX 8 8 390 790 1', native_text('直接批量打印', 92, 36, 3, 2, 2), qr(PRINT_ALL_CODE, 118, 92), native_text(PRINT_ALL_CODE, 132, 282, 3, 2, 2), 'LINE 40 382 360 382 1', native_text('拆箱批量打印', 92, 426, 3, 2, 2), qr(PRINT_ALL_PACK_CODE, 118, 482), native_text(PRINT_ALL_PACK_CODE, 132, 672, 3, 2, 2)])

def fast_cpcl(kind, fields):
    return function_codes_cpcl() if kind == 'function_codes' else inventory_cpcl(fields) if kind == 'inventory' else order_cpcl(fields)
