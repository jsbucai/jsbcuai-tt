import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

STOCK_LOGISTICS = frozenset({"LS库存件", "LD库存件", "LM库存件", "DQ库存件", "QT库存件", "库存件组合"})
ORDER_LOGISTICS = STOCK_LOGISTICS | frozenset({"异常", "需采购", "待到货", "催货", "来货件"})
PRINT_ALL_CODE = "PALL01"
PRINT_ALL_PACK_CODE = "PBOX01"
NOTE_KEYS = ("orderNo", "projectNo", "lineNo", "drawingNo", "name", "spec", "grade", "surface", "quantity", "sku")
DELIVERY_SUFFIX = re.compile(r"(?:^|#)\s*交期\s*[=:：]\s*([^#\s]+)\s*$")


def text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def normalized(value):
    return re.sub(r"[\s_]+", "", text(value)).casefold()


def quantity(value):
    try:
        result = float(text(value).replace(",", ""))
        return result if math.isfinite(result) else 0.0
    except (ValueError, TypeError):
        return 0.0


def _note_text(value):
    return text(value).replace('＃', '#')


def parse_summary(value):
    """Interpret label descriptions only. Never establish product or owner identity.

    Empty positions in the legacy format are meaningful. Repair an extra empty
    position only if exactly one resulting field tuple has a numeric quantity.
    Keep the original text so a normalization can always be inspected.
    """
    raw = str(value or '')
    body = _note_text(value)
    warnings = []
    try:
        delivery = note_delivery_date(value)
        delivery_status = 'known' if delivery else 'missing'
    except ValueError:
        delivery = None
        delivery_status = 'invalid'
    if not delivery:
        warnings.append('交期待确认')
    body = re.sub(r'(?:^|#)\s*交期\s*[=:：].*$', '', body)
    parts = [text(part).replace('_', ' ').strip() for part in body.split('#')]
    def numeric(token):
        try:
            n = Decimal(token.replace(',', ''))
            return n.is_finite() and n >= 0
        except (InvalidOperation, ValueError):
            return False
    repaired = False
    if len(parts) == 11:
        candidates = {tuple(parts[:i] + parts[i+1:]) for i, part in enumerate(parts)
                      if not part and numeric((parts[:i] + parts[i+1:])[8])}
        if len(candidates) == 1:
            parts = list(candidates.pop())
            repaired = True
            warnings.append('摘要多余空字段已按明确位置识别')
    fields = {}
    if len(parts) in (8, 9, 10) and any(parts[:4]):
        fields = dict(zip(NOTE_KEYS, parts))
        tail = fields.get('sku', '')
        if re.fullmatch(r'[\u3400-\u9fff·•]{2,12}', tail):
            fields['summaryPerson'] = tail
            fields['sku'] = ''
        if delivery:
            fields['deliveryDate'] = delivery
    else:
        warnings.append('摘要无完整结构，标签描述采用商品资料' if body else '摘要为空，标签描述采用商品资料')
    return {'fields': fields, 'raw': raw, 'warnings': warnings,
            'delivery_date': delivery, 'delivery_date_status': delivery_status,
            'normalized': repaired or _note_text(value) != raw.strip()}


def parse_note(value):
    return parse_summary(value)['fields']


def note_delivery_date(value):
    """Read an explicit trailing ``#交期=YYYY-MM-DD`` marker.

    A marker is optional for legacy summaries. If a marker is present, it must
    contain a real ISO calendar date; callers can then distinguish malformed
    input from a summary that has not yet been migrated.
    """
    raw = _note_text(value)
    markers = list(re.finditer(r'(?:^|#)\s*交期\s*[=:：]\s*([^#]*)', raw))
    if markers:
        values = set()
        for marker in markers:
            token = marker.group(1).strip()
            match = re.fullmatch(r'(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?', token)
            try:
                if not match:
                    raise ValueError()
                values.add(date(*map(int, match.groups())).isoformat())
            except ValueError:
                raise ValueError('交期待确认：需要明确且有效的完整年月日') from None
        if len(values) != 1:
            raise ValueError('交期待确认：摘要存在多个不同交期')
        return values.pop()
    return None


def split_packages(total, maximum=None):
    try:
        total = Decimal(str(total))
        if not total.is_finite() or total <= 0:
            raise ValueError("数量必须为正数")
        maximum = Decimal(str(maximum)) if maximum not in (None, "", 0, "0") else total
        if not maximum.is_finite() or maximum <= 0:
            raise ValueError("每件数量必须为正数")
        if (total / maximum).to_integral_value(rounding="ROUND_CEILING") > 100:
            raise ValueError("一次最多生成 100 张标签，请分批处理")
        output = []
        while total > 0:
            current = min(total, maximum)
            output.append(float(current))
            total -= current
        return output
    except (InvalidOperation, TypeError):
        raise ValueError("数量格式不正确")


def as_bool(value):
    if isinstance(value, bool):
        return value
    return text(value).lower() in {"1", "true", "yes", "是", "已完成", "重要", "紧急"}


def number_text(value):
    result = quantity(value)
    return str(int(result)) if result.is_integer() else str(result)


def excel_date(value, date_only=False):
    """Normalize serials in known date fields; leave free-form/string dates untouched."""
    result = text(value)
    if re.fullmatch(r"\d+(?:\.\d+)?", result) and 1 <= float(result) < 2958466:
        from openpyxl.utils.datetime import from_excel
        converted = from_excel(float(result))
        if isinstance(converted, datetime):
            return converted.date().isoformat() if date_only else converted.isoformat(timespec="seconds")
    return result[:10] if date_only and re.match(r"^\d{4}-\d{2}-\d{2}", result) else result


def excel_time(value):
    result = text(value)
    if re.fullmatch(r"0(?:\.\d+)?", result):
        from openpyxl.utils.datetime import from_excel
        return from_excel(float(result)).strftime("%H:%M")
    return result
