"""API-only sales submissions: one frozen source line, one durable document.

A saved sending intent is never retried, even after a process crash. Excel and
API projections are derived from the same normalized, confirmed document.
"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
import json
import re
import uuid
from zoneinfo import ZoneInfo
from .store import dumps, timestamp
from .daily_work import _setting
from .sales_ownership import fixed_sales_owner
from .sales_pricing import quote_price
from .sales_web_import import _frozen_draft, _line_document_summary_parts, _detail_note
from .sales_customer_directory import delivery as customer_delivery
from .unit_policy import VALID_UNIT_EVIDENCE


class SalesAPIError(ValueError):
    def __init__(self, code, message):
        self.code=code
        super().__init__(message)


def planned_numbers(draft):
    """ERP business numbers follow the source PO and source line, not UUIDs.

    Only persisted server-derived source_summary is used. Manual rows have no
    source row number and use their reviewed order in the draft. Never truncate
    a PO or silently renumber duplicate source rows.
    """
    po=str(draft.get('customer_order_no') or '').strip()
    if not po or '#' in po or any(ord(c)<32 or ord(c)==127 for c in po):
        raise SalesAPIError('sales_order_number_invalid','请填写有效的客户订单号，不能包含 # 或控制字符')
    lines=draft.get('lines',[])
    if not 1<=len(lines)<=500:
        raise SalesAPIError('sales_line_count_invalid','销售明细须为 1 至 500 行')
    result=[]
    for ordinal,line in enumerate(lines,1):
        source=line.get('source_summary') or {}
        line_no=str(source.get('customer_line_no') or ordinal).strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}',line_no):
            raise SalesAPIError('sales_source_line_number_invalid','原订单行号无法生成唯一单号，请核对原资料行号')
        number=po+'#'+line_no
        if len(number)>200:
            raise SalesAPIError('sales_order_number_too_long','客户订单号加行号超过 200 字，请核对；不会自动截断')
        result.append(number)
    if len({n.casefold() for n in result})!=len(result):
        raise SalesAPIError('sales_source_line_number_duplicate','原订单行号重复，请核对；不会自动重排行号')
    return result


def _reserve_guard(db,profile,numbers):
    """Include legacy UL-numbered submissions in duplicate prevention."""
    wanted={n.casefold() for n in numbers}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_manual_imports'").fetchone():
        for item in db.execute("SELECT model_json FROM sales_manual_imports WHERE profile_id=? AND status!='cancelled'",(profile,)):
            if any(str(d['number']).casefold() in wanted for d in json.loads(item['model_json'])['documents']):
                raise SalesAPIError('sales_order_number_exists','客户订单行已有手动导入表格，请核对人工导入进度，不能重复录入')
    rows=db.execute('''SELECT DISTINCT b.model_json FROM sales_api_batches b
        JOIN sales_api_documents d ON d.batch_id=b.id WHERE d.profile_id=?''',(profile,))
    for row in rows:
        for doc in json.loads(row['model_json']).get('documents',[]):
            candidates={str(doc.get('number','')).casefold()}
            parts=str(doc.get('summary','')).split('#')
            if len(parts)>=3:
                candidates.add((parts[0].strip()+'#'+parts[2].strip()).casefold())
            if candidates & wanted:
                raise SalesAPIError('sales_order_number_exists',
                    '客户订单行已有提交记录（'+str(doc.get('number',''))+'），请核对原单，不能重复录入')


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sales_api_batches(
        id TEXT PRIMARY KEY,session_id TEXT NOT NULL UNIQUE,actor_id TEXT NOT NULL,
        confirmed_hash TEXT NOT NULL,model_json TEXT NOT NULL,status TEXT NOT NULL,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS sales_api_documents(
        id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,profile_id TEXT NOT NULL,
        number TEXT NOT NULL,line_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
        status TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',
        receipt_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,
        UNIQUE(profile_id,number),UNIQUE(batch_id,line_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS sales_api_queue ON sales_api_batches(status,created_at)')


def policy(db):
    value=_setting(db,'sales_api_policy',{})
    return value if isinstance(value,dict) else {}


def normalized(db,row):
    frozen,draft,version,confirmed_hash=_frozen_draft(row)
    owner=fixed_sales_owner(db,row['profile_id'],row['customer_name'])
    if not owner or owner!=frozen.get('fixed_sales_owner_id'):
        raise SalesAPIError('sales_owner_changed','客户归属已变化，请重新核对')
    handler=draft.get('handler_name')
    if handler not in {'李瑄','刘锋'}:
        raise SalesAPIError('sales_handler_required','请选择经手人李瑄或刘锋')
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    numbers=planned_numbers(draft)
    documents=[]
    for index,(line,number) in enumerate(zip(draft['lines'],numbers),1):
        identity=line.get('resolved_identity',{})
        if (identity.get('profile_id')!=row['profile_id'] or identity.get('warehouse_id')!='29871293290508390'
            or identity.get('base_unit')!='1' or line.get('unit')!='支'
            or line.get('unit_evidence') not in VALID_UNIT_EVIDENCE or line.get('issues')):
            raise SalesAPIError('sales_identity_review','商品、仓库或计量单位仍需核对')
        delivery=line.get('delivery_date') or draft.get('delivery_date')
        if not delivery or delivery<today:
            raise SalesAPIError('sales_delivery_review','交期须不早于录单日期')
        unit,amount=quote_price(line['quantity'],line['unit_price'],line['tax_basis'])
        if (unit,amount)!=(line['untaxed_unit_price'],line['untaxed_line_amount']):
            raise SalesAPIError('sales_price_changed','税价计算与确认稿不一致')
        summary=_detail_note(_line_document_summary_parts(draft,line,index))+'#交期='+delivery
        tax=(Decimal(amount)*Decimal('.13')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        documents.append({'number':number,'line_id':line['id'],'ordinal':index,
            'product_code':line['product_code'],'product_name':line['product_name'],
            'identity':identity,'quantity':line['quantity'],'unit':'支','unit_price':unit,
            'amount':amount,'tax_amount':format(tax,'.2f'),'gross_amount':format(Decimal(amount)+tax,'.2f'),
            'delivery_date':delivery,'summary':summary})
    return {'schema':'sales_api_v2','session_id':row['id'],'version':version,'confirmed_hash':confirmed_hash,
        'number_rule':'customer_po_source_line',
        'customer_delivery':customer_delivery(db,row['profile_id'],row['erp_customer_id'],row['customer_name']),
        'delivery_write_status':'not_verified',
        'profile_id':row['profile_id'],'customer_name':row['customer_name'],'fixed_owner_id':owner,
        'erp_customer_id':row['erp_customer_id'],
        'handler_name':handler,'maker_name':'罗健树','warehouse_name':'江苏优螺',
        'warehouse_id':'29871293290508390','order_date':today,'tax_rate':13,'documents':documents}


def enqueue(db,row,actor):
    config=policy(db)
    if config.get('enabled') is not True or config.get('contract_accepted') is not True:
        raise SalesAPIError('sales_api_not_accepted','API 尚在合同验收，确认稿已保存，可以导出核对表')
    if config.get('profile_id')!=row['profile_id']:
        raise SalesAPIError('sales_api_scope','API 账套与草稿不一致')
    if actor!=config.get('owner_user_id','admin'):
        raise SalesAPIError('sales_api_owner_only','当前仅本人可提交 API 订单')
    model=normalized(db,row)
    _reserve_guard(db,row['profile_id'],[d['number'] for d in model['documents']])
    batch=uuid.uuid4().hex;now=timestamp()
    db.execute('INSERT INTO sales_api_batches VALUES (?,?,?,?,?,?,?,?)',
        (batch,row['id'],actor,row['confirmed_hash'],dumps(model),'queued',now,now))
    for doc in model['documents']:
        db.execute('''INSERT INTO sales_api_documents
            (id,batch_id,profile_id,number,line_id,ordinal,status,updated_at) VALUES (?,?,?,?,?,?,'queued',?)''',
            (uuid.uuid4().hex,batch,row['profile_id'],doc['number'],doc['line_id'],doc['ordinal'],now))
    return batch,model['documents'][0]['number']


def projection(db,session_id):
    batch=db.execute('SELECT * FROM sales_api_batches WHERE session_id=?',(session_id,)).fetchone()
    if not batch:return None
    docs=[dict(r) for r in db.execute('''SELECT number,line_id,ordinal,status,receipt_json,updated_at
        FROM sales_api_documents WHERE batch_id=? ORDER BY ordinal''',(batch['id'],))]
    for doc in docs:doc['receipt']=json.loads(doc.pop('receipt_json'))
    return {'id':batch['id'],'status':batch['status'],'documents':docs,'updated_at':batch['updated_at'],
            'completion_basis':'api_receipt','erp_readback_performed':False,
            'delivery_write_status':'not_verified',
            'delivery_message':'当前销售 API 尚未验证发货信息写入，请在 ERP 原单核对并补齐；不要重新提交订单'}


def workbook(model):
    from openpyxl import Workbook
    book=Workbook();sheet=book.active;sheet.title='确认录单'
    sheet.append(['ERP单号','客户','经手人','制单人','商品编码','商品名称','数量','单位','未税单价','未税金额','税额','含税金额','交期','摘要'])
    for doc in model['documents']:
        # String cells intentionally keep exact decimals and block Excel formulas.
        values=[doc['number'],model['customer_name'],model['handler_name'],model['maker_name'],
            doc['product_code'],doc['product_name'],doc['quantity'],'支',doc['unit_price'],doc['amount'],
            doc['tax_amount'],doc['gross_amount'],doc['delivery_date'],doc['summary']]
        sheet.append(values)
        for cell in sheet[sheet.max_row]:cell.data_type='s'
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    output=BytesIO();book.save(output);output.seek(0);return output


def decimal_json(value):
    """Encode exact numeric ERP fields without binary-float rounding."""
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('nonfinite')
        return format(value,'f')
    if isinstance(value,dict):return '{'+','.join(json.dumps(k)+':'+decimal_json(v) for k,v in value.items())+'}'
    if isinstance(value,list):return '['+','.join(map(decimal_json,value))+']'
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def wire_document(model,document,binding):
    def identity(value):
        value=str(value)
        if not value.isdecimal() or not 0<=int(value)<2**64:raise ValueError('invalid ERP identity')
        return int(value)
    product=binding['products'][document['product_code']];sku=product['sku']
    if str(product['ptypeid'])!=document['identity']['product_id'] or str(sku['skuid'])!=document['identity']['sku_id']:
        raise SalesAPIError('sales_product_changed','ERP 商品身份与已确认明细不一致')
    return {'number':document['number'],'date':model['order_date']+' 00:00:00',
        'todate':document['delivery_date']+' 00:00:00','total':Decimal(document['gross_amount']),
        'ktypeid':identity(model['warehouse_id']),'btypeid':identity(binding['customer_id']),
        'etypeid':identity(binding['handler_id']),'summary':document['summary'],
        'details':[{'ptypeid':identity(product['ptypeid']),'pfullname':product['fullname'],
            'prop1':identity(sku['prop1']),'prop2':identity(sku['prop2']),'unit':1,'unitrate':1,
            'assqty':Decimal(document['quantity']),'assdpprice':Decimal(document['unit_price']),
            'asstpprice':Decimal(document['unit_price']),'tax':13,'comment':document['summary']}]}
