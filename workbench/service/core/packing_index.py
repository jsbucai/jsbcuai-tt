"""Rebuildable packaging read model, separate from durable human/print facts.

Dirty marks participate in the source transaction. Refresh publishes all rows
atomically; printing never dirties this source model. A response reads its index
and task/print facts in the same SQLite snapshot.
"""
import json
from collections import defaultdict
from .store import dumps
from .daily_work import _text, _json, _customer, _day, _hash, _in_unfulfilled_scope
from .rules import parse_note

SOURCE_TABLES = ('records','logistics_calculations','sales_customer_owners',
                 'customer_order_group_aliases','customer_order_followups','staff_users')

class PackingIndex:
    def __init__(self, store, customers):
        self.store,self.customers=store,customers
        with store.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS packing_index_meta(id INTEGER PRIMARY KEY CHECK(id=1),dirty INTEGER NOT NULL,version INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO packing_index_meta VALUES (1,1,0)')
            db.execute('''CREATE TABLE IF NOT EXISTS packing_order_index(
                profile_id TEXT NOT NULL,order_no TEXT NOT NULL,customer_key TEXT NOT NULL,
                customer TEXT NOT NULL,order_date TEXT NOT NULL,order_search TEXT NOT NULL,
                skus TEXT NOT NULL,rows_json TEXT NOT NULL,inventory_json TEXT NOT NULL,groups_json TEXT NOT NULL,
                PRIMARY KEY(profile_id,order_no))''')
            db.execute('CREATE INDEX IF NOT EXISTS packing_index_customer ON packing_order_index(customer_key,order_date)')
            db.execute('CREATE INDEX IF NOT EXISTS packing_index_date ON packing_order_index(order_date,order_no)')
            existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in SOURCE_TABLES:
                if table not in existing:continue
                for event in ('INSERT','UPDATE','DELETE'):
                    condition=''
                    if table=='records':
                        names=[]
                        if event!='DELETE':names.append("NEW.kind IN ('orders','inventory')")
                        if event!='INSERT':names.append("OLD.kind IN ('orders','inventory')")
                        condition=' WHEN '+' OR '.join(names)
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS packing_index_dirty_{table}_{event.lower()}
                        AFTER {event} ON {table}{condition} BEGIN UPDATE packing_index_meta SET dirty=1 WHERE id=1; END''')

    def refresh(self, db=None):
        if db is None:
            with self.store.connect() as reader:
                if not reader.execute('SELECT dirty FROM packing_index_meta WHERE id=1').fetchone()[0]:return
            with self.store.connect(write=True) as writer:self.refresh(writer)
            return
        if not db.execute('SELECT dirty FROM packing_index_meta WHERE id=1').fetchone()[0]:return
        groups=self.customers._build(db)[0]
        by_order=defaultdict(list)
        keep=('id','profile_id','customer','customer_order_no','assignee_id','customer_review','owner_review','missing_count','due_date_from')
        for group in groups.values():
            slim={k:group.get(k) for k in keep}
            for line in group['lines']:by_order[(group['profile_id'],line['order_no'])].append(slim)
        inventory={}
        identity=lambda r:tuple(_text(r.get(k)) for k in ('profile_id','warehouse_id','product_id','sku_id','base_unit'))
        for row in db.execute("SELECT payload FROM records WHERE kind='inventory'"):
            item=_json(row[0],{});inventory[identity(item)]=item
        orders=defaultdict(list)
        for row in db.execute("SELECT payload FROM records WHERE kind='orders'"):
            item=_json(row[0],{});orders[(_text(item.get('profile_id')),_text(item.get('order_no')))].append(item)
        db.execute('DELETE FROM packing_order_index')
        values=[]
        for (profile,number),rows in orders.items():
            names={_customer(row) for row in rows}
            customer=next(iter(names)) if len(names)==1 else ''
            pos={_text(g.get('customer_order_no')) for g in by_order[(profile,number)]}
            for row in rows:
                note=parse_note(row.get('summary') or row.get('system_note')) or {}
                pos.add(_text(row.get('customer_order_no')));pos.add(_text(note.get('orderNo')))
            source_inventory={identity(row):inventory.get(identity(row),{}) for row in rows}
            values.append((profile,number,_hash([profile,customer]) if customer else '',customer,
                min((_day(r.get('order_date') or r.get('document_date')) for r in rows if _day(r.get('order_date') or r.get('document_date'))),default=''),
                '\n'.join([number,*sorted(pos)]).casefold(),dumps(sorted({_text(r.get('sku')) for r in rows})),
                dumps(rows),dumps(list(source_inventory.values())),dumps(by_order[(profile,number)])))
        db.executemany('INSERT INTO packing_order_index VALUES (?,?,?,?,?,?,?,?,?,?)',values)
        db.execute('UPDATE packing_index_meta SET dirty=0,version=version+1 WHERE id=1')

    def inputs(self, db, role, actor, view, query):
        # Index filtering is on whole orders. A supplier filter never silently
        # turns one complete-order label job into a partial quantity print.
        where=["t.role=?","t.condition_state='active'"];args=[role]
        if view=='mine':
            where.append("(t.assignee_id=? OR (t.role='arrival_packing' AND t.task_type='arrival_shortage_waiting' AND t.assignee_id=''))");args.append(actor)
        if query['customer_key']:where.append('i.customer_key=?');args.append(query['customer_key'])
        if query['customer']:where.append('i.customer=?');args.append(query['customer'])
        if query['sku']:
            where.append("EXISTS(SELECT 1 FROM json_each(i.skus) WHERE value=?)");args.append(query['sku'])
        if query['order_no'] and query['order_no_match']=='contains':
            where.append('instr(i.order_search,?)>0');args.append(query['order_no'].casefold())
        for field,op in (('order_date_from','>='),('order_date_to','<=')):
            if query[field]:where.append('i.order_date'+op+'?');args.append(query[field])
        found=db.execute('''SELECT t.*,i.rows_json,i.inventory_json,i.groups_json FROM daily_work_tasks t
            JOIN packing_order_index i ON i.profile_id=t.profile_id AND i.order_no=t.order_no
            WHERE '''+' AND '.join(where),args)
        source,inventory,groups,by_order,tasks={},{},{},defaultdict(list),defaultdict(list)
        for row in found:
            rows = [item for item in _json(row['rows_json'], []) if _in_unfulfilled_scope(item)]
            if not rows:
                continue
            task={k:row[k] for k in row.keys() if k not in {'rows_json','inventory_json','groups_json'}}
            task['payload']=_json(task['payload'],{});tasks[role].append(task)
            ref=(row['profile_id'],row['order_no']);source[ref]=rows
            for item in _json(row['inventory_json'],[]):
                key=tuple(_text(item.get(k)) for k in ('profile_id','warehouse_id','product_id','sku_id','base_unit'))
                inventory[key]=item
            for group in _json(row['groups_json'],[]):
                groups[group['id']]=group;by_order[ref].append(group)
        return source,inventory,groups,by_order,tasks
