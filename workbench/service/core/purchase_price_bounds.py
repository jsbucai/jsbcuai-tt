"""Private, snapshot-bound WMS sale-price facts and conservative purchase caps.

`total / assqty` is the observed inclusive-price candidate. The 25 matched ERP
sales-report rows are the evidence policy; `tax=1` is NOT a 13% tax rate. No
Qimen sell_price, historical price, WMS price/assprice, or guessed conversion is
used. Platform comparison still needs its separately verified inclusive basis.
The user confirmed all ERP base-unit-1 labels mean 支 on 2026-10-07. Raw ERP
names remain in public records and WMS cache; the fact hash binds all three
names without changing the persisted fact schema needed for program rollback.
Platform counting/packaging still needs review before any payment.
"""
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from .store import dumps
from .unit_policy import (CONFIRMED_BASE_UNIT, USER_UNIT_EVIDENCE,
    USER_OVERRIDE_UNIT_EVIDENCE, ERP_UNIT_EVIDENCE, VALID_UNIT_EVIDENCE, unit_evidence)

IDENTITY_FIELDS=('profile_id','warehouse_id','product_id','sku_id','base_unit')
TAX_BASIS='tax_inclusive'
TAX_EVIDENCE='wms_total_25matched_sales_rows_v1'


def text(value): return '' if value is None else str(value).strip()


def identity(row): return '|'.join(text(row.get(key)) for key in IDENTITY_FIELDS)


def decimal(value,positive=False):
    if value is None or isinstance(value,bool) or len(str(value))>80: raise ValueError('price_number_invalid')
    try: result=Decimal(str(value))
    except InvalidOperation: raise ValueError('price_number_invalid') from None
    if not result.is_finite() or result<0 or positive and not result or result.adjusted()>15 or result.as_tuple().exponent < -12:
        raise ValueError('price_number_invalid')
    return result


def number(value): return format(value.normalize(),'f')


def match_wms_lines(items,details):
    """Resolve one-to-one lines, preferring stable IDs over equal quantities.

    Same-product split lines or duplicate equal quantities without unique IDs
    are ambiguous. One WMS row cannot supply evidence to two Qimen lines.
    """
    goods=details.get('goodslist',[]) if isinstance(details,dict) else []
    goods=goods if isinstance(goods,list) else []
    resolved=[]
    for item in items:
        code=text(item.get('spec_code') or item.get('goods_code')).upper()
        source_id=text(item.get('recid') or item.get('oid'))
        by_id=[index for index,line in enumerate(goods) if isinstance(line,dict) and source_id not in {'','0'} and
               source_id in {text(line.get('id')),text(line.get('sourcedetailid'))}]
        candidates=by_id if by_id else list(range(len(goods)))
        matched=[]
        for index in candidates:
            line=goods[index]
            if not isinstance(line,dict): continue
            try:
                valid=(text(line.get('usercode')).upper()==code and
                       decimal(line.get('assqty',line.get('qty')),True)==decimal(item.get('sell_count'),True))
            except ValueError: valid=False
            if valid: matched.append(index)
        # IDs themselves must be unique; do not choose one half of a split.
        resolved.append(matched[0] if len(matched)==1 and (not by_id or len(by_id)==1) else None)
    used=Counter(index for index in resolved if index is not None)
    return [goods[index] if index is not None and used[index]==1 else {} for index in resolved]


def sale_price_fact(record,line,head,inventory,source_updated_at):
    try:
        parts=[text(record.get(key)) for key in IDENTITY_FIELDS]
        if not all(parts) or parts[-1]!='1': return None
        if text(inventory.get('base_unit'))!=parts[-1]: return None
        if text(head.get('tradeid'))!=text(record.get('order_no')): return None
        if text(line.get('ptypeid'))!=parts[2] or text(line.get('skuid'))!=parts[3]: return None
        if text(line.get('ktypeid') or head.get('ktypeid'))!=parts[1]: return None
        if text(line.get('unit'))!='1' or decimal(line.get('unitrate'),True)!=1: return None
        quantity=decimal(record.get('quantity'),True)
        if decimal(line.get('assqty'),True)!=quantity or decimal(line.get('qty'),True)!=quantity: return None
        # The API documents call this a tax field but default it to 1. Only the
        # observed contract is supported; it cannot prove another tax regime.
        if decimal(line.get('tax'),True)!=1: return None
        amount=decimal(line.get('total'),True)
        raw_unit_names={'wms':text(line.get('unitname')),'order':text(record.get('unit_name')),
            'inventory':text(inventory.get('unit_name'))}
        # Names are audit evidence, not conversion factors. Unit ID, rate and
        # quantity have already been checked independently above.
        evidence=unit_evidence(raw_unit_names.values())
        if evidence is None: return None
        unit=CONFIRMED_BASE_UNIT
        source_doc=text(head.get('vchcode'));source_line=text(line.get('id') or line.get('sourcedetailid'))
        if source_doc in {'','0'} or source_line in {'','0'} or not text(record.get('line_id')): return None
        raw={key:line.get(key) for key in ('id','sourcedetailid','ptypeid','skuid','usercode','ktypeid',
             'unit','unitname','unitrate','qty','assqty','total','price','assprice','tax') if key in line}
        facts={'profile_id':parts[0],'order_no':record['order_no'],'line_id':record['line_id'],
            'product_identity':identity(record),'erp_code':record['sku'],'quantity':number(quantity),
            'inclusive_amount':number(amount),'unit':unit,'unit_evidence':evidence,
            'raw_unit_names':dumps(raw_unit_names),
            'tax_basis':TAX_BASIS,'tax_evidence':TAX_EVIDENCE,
            'raw_tax':number(decimal(line['tax'])),'source_doc_id':source_doc,'source_line_id':source_line,
            'source_updated_at':source_updated_at}
        facts['source_hash']=sha256(dumps({'fact':facts,'raw':raw}).encode()).hexdigest()
        return facts
    except (ValueError,KeyError,TypeError): return None


def validate_sale_price_facts(snapshot):
    facts=snapshot.get('wms_sale_price_facts',[])
    if not isinstance(facts,list) or any(not isinstance(fact,dict) for fact in facts):
        raise ValueError('销售价格事实集合无效')
    published={(text(row.get('order_no')),text(row.get('line_id'))):row for row in snapshot['records']['orders']}
    inventory={identity(row):row for row in snapshot['records'].get('inventory',[]) if isinstance(row,dict)}
    seen=set();used=set()
    required={'profile_id','order_no','line_id','product_identity','erp_code','quantity','inclusive_amount',
              'unit','unit_evidence','raw_unit_names','tax_basis','tax_evidence','raw_tax',
              'source_doc_id','source_line_id','source_hash','source_updated_at'}
    for fact in facts:
        key=(text(fact.get('order_no')),text(fact.get('line_id')))
        source=(fact.get('source_doc_id'),fact.get('source_line_id'))
        row=published.get(key)
        if not required<=fact.keys() or not row or key in seen or source in used:
            raise ValueError('销售价格事实明细身份不唯一')
        try: raw_unit_names=json.loads(fact.get('raw_unit_names',''))
        except (TypeError,ValueError): raw_unit_names=None
        if (not isinstance(raw_unit_names,dict) or set(raw_unit_names)!={'wms','order','inventory'} or
            any(not isinstance(value,str) for value in raw_unit_names.values()) or
            raw_unit_names['order']!=text(row.get('unit_name')) or
            (fact.get('product_identity') in inventory and
                raw_unit_names['inventory']!=text(inventory[fact['product_identity']].get('unit_name'))) or
            unit_evidence(raw_unit_names.values())!=fact['unit_evidence'] or
            not all(text(row.get(key)) for key in IDENTITY_FIELDS) or row.get('base_unit')!='1' or
            fact['profile_id']!=snapshot['profile_id'] or fact['product_identity']!=identity(row) or
            fact['erp_code']!=row['sku'] or decimal(fact['quantity'],True)!=decimal(row['quantity'],True) or
            fact['tax_basis']!=TAX_BASIS or fact['tax_evidence']!=TAX_EVIDENCE or
            decimal(fact['raw_tax'],True)!=1 or fact['unit']!=CONFIRMED_BASE_UNIT or
            fact['unit_evidence'] not in VALID_UNIT_EVIDENCE or
            text(fact['source_doc_id']) in {'','0'} or text(fact['source_line_id']) in {'','0'} or
            fact['source_updated_at']!=snapshot['source_updated_at']):
            raise ValueError('销售价格事实与当前订单／税价依据不一致')
        decimal(fact['inclusive_amount'],True)
        if len(text(fact['source_hash']))!=64 or any(char not in '0123456789abcdef' for char in fact['source_hash']):
            raise ValueError('销售价格事实哈希无效')
        seen.add(key);used.add(source)
    return facts


def ensure_fact_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS wms_sale_price_facts(
        data_version TEXT NOT NULL,profile_id TEXT NOT NULL,order_no TEXT NOT NULL,line_id TEXT NOT NULL,
        product_identity TEXT NOT NULL,erp_code TEXT NOT NULL,quantity TEXT NOT NULL,
        inclusive_amount TEXT NOT NULL,unit TEXT NOT NULL,tax_basis TEXT NOT NULL,tax_evidence TEXT NOT NULL,
        raw_tax TEXT NOT NULL,source_doc_id TEXT NOT NULL,source_line_id TEXT NOT NULL,
        source_hash TEXT NOT NULL,source_updated_at TEXT NOT NULL,unit_evidence TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(data_version,profile_id,order_no,line_id),
        UNIQUE(data_version,profile_id,source_doc_id,source_line_id))''')
    columns={row['name'] for row in db.execute('PRAGMA table_info(wms_sale_price_facts)')}
    if 'unit_evidence' not in columns:
        # Preserve old facts without falsely assigning provenance; an empty
        # evidence marker cannot supply a cap until a new verified collection.
        db.execute("ALTER TABLE wms_sale_price_facts ADD COLUMN unit_evidence TEXT NOT NULL DEFAULT ''")


def persist_sale_price_facts(db,snapshot,version):
    ensure_fact_schema(db)
    facts=validate_sale_price_facts(snapshot)
    db.execute('DELETE FROM wms_sale_price_facts WHERE profile_id=?',(snapshot['profile_id'],))
    for fact in facts:
        db.execute('INSERT INTO wms_sale_price_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (version,*[fact[key] for key in ('profile_id','order_no','line_id','product_identity','erp_code',
                'quantity','inclusive_amount','unit','tax_basis','tax_evidence','raw_tax','source_doc_id',
                'source_line_id','source_hash','source_updated_at','unit_evidence')]))


def refresh_price_bounds(store):
    """Atomically replace caps from current tasks and current private facts only.

    The full current demand quantities weight selling prices, not just this
    task's shortage. Any uncovered/ambiguous line withholds the whole task cap.
    This function never reads ERP or copies price information into public rows.
    """
    with store.connect(write=True) as db:
        ensure_fact_schema(db)
        db.execute('''CREATE TABLE IF NOT EXISTS purchase_price_bounds(
            task_id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,erp_code TEXT NOT NULL,
            product_identity TEXT NOT NULL,data_version TEXT NOT NULL,unit TEXT NOT NULL,
            weighted_sale_price TEXT NOT NULL,price_limit TEXT NOT NULL,tax_basis TEXT NOT NULL,
            source_hash TEXT NOT NULL,source_updated_at TEXT NOT NULL)''')
        columns={row['name'] for row in db.execute('PRAGMA table_info(purchase_price_bounds)')}
        if 'task_id' not in columns: raise ValueError('采购价格上限需要 task_id 版本合同迁移')
        version_row=db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
        version=json.loads(version_row[0]) if version_row else ''
        source_row=db.execute("SELECT value FROM settings WHERE key='data_source_updated_at'").fetchone()
        source_updated=json.loads(source_row[0]) if source_row else ''
        projected=db.execute("SELECT value FROM daily_work_settings WHERE key='projection_data_version'").fetchone()
        projected_at=db.execute("SELECT value FROM daily_work_settings WHERE key='projection_source_updated_at'").fetchone()
        db.execute('DELETE FROM purchase_price_bounds')
        if not version or not projected or projected[0]!=version or not projected_at or projected_at[0]!=source_updated:
            return {'published':0,'review':0,'data_version':version,'status':'tasks_not_current'}
        rows=defaultdict(list)
        for record in db.execute("SELECT payload FROM records WHERE kind='orders'"):
            row=json.loads(record[0]);rows[(text(row.get('profile_id')),text(row.get('order_no')),identity(row))].append(row)
        facts={(row['profile_id'],row['order_no'],row['line_id']):dict(row) for row in db.execute(
            'SELECT * FROM wms_sale_price_facts WHERE data_version=?',(version,))}
        published=review=0
        for task in db.execute("SELECT * FROM daily_work_tasks WHERE role='purchase' AND condition_state='active' AND status IN ('todo','in_progress','waiting')"):
            payload=json.loads(task['payload']);product_identity=text(payload.get('product_identity'))
            refs=payload.get('order_nos');parts=product_identity.split('|')
            selected=[];valid=bool(isinstance(refs,list) and refs and len(refs)==len(set(refs)) and
                len(parts)==5 and all(parts) and parts[0]==task['profile_id'] and task['source_data_version']==version and
                not payload.get('read_only') and not payload.get('source_scope_review'))
            if valid:
                for ref in refs:
                    lines=rows.get((task['profile_id'],text(ref),product_identity),[])
                    if not lines: valid=False;break
                    for line in lines:
                        fact=facts.get((task['profile_id'],text(ref),text(line.get('line_id'))))
                        try:
                            ready=(fact and fact['product_identity']==product_identity and fact['erp_code']==task['sku'] and
                                text(line.get('sku'))==task['sku'] and line.get('calculation_status')=='ready' and
                                not line.get('shadow_demand') and not line.get('lifecycle_pending_review') and
                                decimal(fact['quantity'],True)==decimal(line.get('quantity'),True) and
                                fact['tax_basis']==TAX_BASIS and fact['tax_evidence']==TAX_EVIDENCE and
                                fact['unit']==CONFIRMED_BASE_UNIT and
                                fact['unit_evidence'] in VALID_UNIT_EVIDENCE and
                                (fact['unit_evidence']!=USER_UNIT_EVIDENCE or not text(line.get('unit_name'))) and
                                (fact['unit_evidence']!=ERP_UNIT_EVIDENCE or text(line.get('unit_name')) in {'',CONFIRMED_BASE_UNIT}) and
                                decimal(fact['raw_tax'],True)==1 and fact['source_updated_at']==source_updated and
                                parts[-1]=='1')
                            if not ready: valid=False;break
                            decimal(fact['inclusive_amount'],True);selected.append(fact)
                        except (TypeError,ValueError): valid=False;break
                    if not valid: break
            if not valid or not selected or len({fact['unit'] for fact in selected})!=1:
                review+=1;continue
            amount=sum((decimal(fact['inclusive_amount'],True) for fact in selected),Decimal(0))
            quantity=sum((decimal(fact['quantity'],True) for fact in selected),Decimal(0))
            weighted=amount/quantity
            # Include the actual cap inputs as well as the raw-evidence hash.
            # A private amount correction need not change the public order
            # version, but must invalidate any frozen procurement proposal.
            source_hash=sha256(dumps(sorted((fact['order_no'],fact['line_id'],fact['source_hash'],
                fact['quantity'],fact['inclusive_amount'],fact['unit'],fact['tax_basis'],
                fact['tax_evidence'],fact['unit_evidence'],fact['raw_tax'],fact['product_identity'],
                fact['source_updated_at']) for fact in selected)).encode()).hexdigest()
            db.execute('INSERT INTO purchase_price_bounds VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (task['id'],task['profile_id'],task['sku'],product_identity,version,selected[0]['unit'],
                 number(weighted),number(weighted*Decimal('0.8')),TAX_BASIS,source_hash,source_updated))
            published+=1
        return {'published':published,'review':review,'data_version':version,'status':'ready'}
