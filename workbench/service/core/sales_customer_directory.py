"""Complete ERP party directory, separate from human-confirmed sales ownership."""
from collections import Counter
from datetime import datetime
import json
from .store import timestamp, dumps
from .sales_ownership import fixed_sales_owner
from .sales_web_customer_preflight import _active, _erp_id


def ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sales_erp_customers(
        profile_id TEXT NOT NULL,erp_id TEXT NOT NULL,customer_name TEXT NOT NULL,
        usercode TEXT NOT NULL,namepy TEXT NOT NULL,active INTEGER NOT NULL,
        category TEXT NOT NULL,PRIMARY KEY(profile_id,erp_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS sales_erp_customer_name ON sales_erp_customers(profile_id,customer_name)')
    db.execute('''CREATE TABLE IF NOT EXISTS sales_erp_customer_sync(
        profile_id TEXT PRIMARY KEY,last_success TEXT NOT NULL DEFAULT '',
        last_attempt TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
        requested INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0)''')
    db.execute('''CREATE TABLE IF NOT EXISTS sales_erp_customer_delivery(
        profile_id TEXT NOT NULL,erp_id TEXT NOT NULL,payload TEXT NOT NULL,
        observed_at TEXT NOT NULL,PRIMARY KEY(profile_id,erp_id))''')


DELIVERY_FIELDS=('id','btypeid','receiverpeople','receivertelephone','receivercellphone',
                 'receiverzipcode','province','city','district','receiveraddress')


def delivery_snapshot(row):
    """Keep only the named party's verified default; no company-address fallback."""
    info=row.get('deliveryinfo')
    default=str(row.get('deliveryinfoid') or '')
    if not isinstance(info,dict) or not default or default=='0':
        return {'status':'missing','info':{}}
    if str(info.get('id'))!=default or str(info.get('btypeid'))!=_erp_id(row):
        return {'status':'conflict','info':{}}
    return {'status':'available','info':{k:str(info.get(k) or '') for k in DELIVERY_FIELDS}}


def delivery(db,profile,erp_id,customer_name):
    empty={'status':'not_loaded','info':{},'observed_at':'','source':'erp.btype.list'}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='sales_erp_customer_delivery'").fetchone():
        return empty
    rows=db.execute('''SELECT c.erp_id,d.payload,d.observed_at FROM sales_erp_customers c
        LEFT JOIN sales_erp_customer_delivery d ON d.profile_id=c.profile_id AND d.erp_id=c.erp_id
        WHERE c.profile_id=? AND c.customer_name=? AND c.active=1''',(profile,customer_name)).fetchall()
    if len(rows)!=1 or erp_id and rows[0]['erp_id']!=str(erp_id):
        return {**empty,'status':'identity_review'}
    row=rows[0]
    if not row['payload']:return empty
    return {**empty,**json.loads(row['payload']),'observed_at':row['observed_at']}


def refresh(store,client,profile):
    """Read all pages before publishing; errors keep the last complete directory."""
    attempted=timestamp()
    with store.connect(write=True) as db:
        ensure(db)
        db.execute('INSERT OR IGNORE INTO sales_erp_customer_sync(profile_id) VALUES (?)',(profile,))
        db.execute("UPDATE sales_erp_customer_sync SET last_attempt=?,requested=0 WHERE profile_id=?",(attempted,profile))
    try:
        if client.account()!=profile:raise ValueError('ERP 账套不一致')
        rows=client.pages('erp.btype.list',{},'btypes',_erp_id,size=100,limit=200)
        if not rows:raise ValueError('客户资料返回空目录，保留上次完整名录')
        values=[];deliveries=[];seen=set()
        for row in rows:
            identity=_erp_id(row);name=str(row.get('fullname') or '').strip()
            if identity in seen or not name:raise ValueError('客户身份重复或名称缺失')
            seen.add(identity)
            values.append((profile,identity,name,str(row.get('usercode') or ''),
                           str(row.get('namepy') or ''),int(_active(row)),str(row.get('bcategory',''))))
            deliveries.append((profile,identity,dumps(delivery_snapshot(row)),attempted))
        with store.connect(write=True) as db:
            db.execute('DELETE FROM sales_erp_customers WHERE profile_id=?',(profile,))
            db.executemany('INSERT INTO sales_erp_customers VALUES (?,?,?,?,?,?,?)',values)
            db.execute('DELETE FROM sales_erp_customer_delivery WHERE profile_id=?',(profile,))
            db.executemany('INSERT INTO sales_erp_customer_delivery VALUES (?,?,?,?)',deliveries)
            db.execute("UPDATE sales_erp_customer_sync SET last_success=?,error='',total=? WHERE profile_id=?",(timestamp(),len(values),profile))
        return {'ok':True,'total':len(values),'active':sum(r[5] for r in values)}
    except Exception:
        with store.connect(write=True) as db:
            db.execute('UPDATE sales_erp_customer_sync SET error=? WHERE profile_id=?',
                       ('ERP 客户资料更新未完成，保留上次完整名录；稍后重试',profile))
        raise


def needed(db,profile,now):
    row=db.execute('SELECT * FROM sales_erp_customer_sync WHERE profile_id=?',(profile,)).fetchone()
    if row and row['requested']:return True
    if not (7*60+30<=now.hour*60+now.minute<19*60):return False
    if not row or not row['last_attempt']:return True
    elapsed=(now.timestamp()-datetime.fromisoformat(row['last_attempt']).timestamp())
    return elapsed >= (900 if row['error'] else 3600)


def choices(db,profile,query='',page=1):
    if not profile or len(profile)>100 or len(query)>200 or not 1<=page<=100000:
        raise ValueError('客户查询参数无效')
    state=db.execute('SELECT * FROM sales_erp_customer_sync WHERE profile_id=?',(profile,)).fetchone()
    rows=[dict(row) for row in db.execute('''SELECT * FROM sales_erp_customers
        WHERE profile_id=? AND active=1 ORDER BY customer_name,erp_id''',(profile,))]
    duplicates=Counter(r['customer_name'] for r in rows)
    search=query.strip().casefold()
    rows=[r for r in rows if any(search in r[k].casefold() for k in ('customer_name','usercode','namepy'))]
    total=len(rows);items=[]
    for row in rows[(page-1)*50:page*50]:
        owner=fixed_sales_owner(db,profile,row['customer_name'])
        ownership=db.execute('SELECT version FROM sales_customer_owners WHERE profile_id=? AND customer_name=?',
                             (profile,row['customer_name'])).fetchone()
        person=db.execute('SELECT display_name FROM staff_users WHERE id=?',(owner,)).fetchone()
        duplicate=duplicates[row['customer_name']]>1
        items.append({**{k:row[k] for k in ('erp_id','customer_name','usercode')},
            'owner_id':owner,'owner_name':person[0] if person else '',
            'owner_version':ownership[0] if ownership else 0,'can_assign_owner':not owner and not duplicate,
            'can_start':bool(owner) and not duplicate,
            'review_reason':'ERP 存在同名单位，请先核对身份' if duplicate else
                           '请先选择并保存客户固定销售' if not owner else ''})
    return {'items':items,'total':total,'page':page,'has_more':page*50<total,
            'sync':dict(state) if state else {'last_success':'','error':'客户名录正在准备'}}
