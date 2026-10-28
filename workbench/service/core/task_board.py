"""Compact, complete role boards over the published local generation.

No ERP calls occur here. Frozen selections and batch mutations share a SQLite
write transaction with the existing task/print ownership checks.
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import Blueprint, g, jsonify, request

from .board_materialization import BoardMaterialization
from .procurement_projection import purchase_identity, purchase_totals, needs_procurement
from .daily_work import (DailyWorkConflict, PACKING_ROLES, ROLES, _text, _json,
    _flag, _roles, _is_admin, _user_id, _setting, _source_is_today, _day,
    _next_workday, _hash, _decimal, _record_identity, _published_order_groups, _customer,
    _lifecycle_review_reason, _print_state_version, PRINT_REVIEW_BLOCK, _current_task)
from .daily_work import SHANGHAI
from .order_print_state import natural_location_key
from .rules import parse_note
from .store import dumps, timestamp

PAGE_SIZE = 50
TWO_STAGE_ROLES = frozenset({'sales','stock_packing','arrival_packing'})
PACKING_FILTERS = ('customer','sku','order_no','keyword','order_date_from','order_date_to',
                   'due_from','due_to','print_state','assignee_id','status','sort',
                   'customer_key','order_no_match','supplier','receipt_from','receipt_to')


def _packing_query(role, packing_mode='', **values):
    mode=_text(packing_mode)
    if mode not in {'','print'} or mode and role not in PACKING_ROLES:
        raise ValueError('打印视图仅适用于包装岗位')
    if not mode:return None
    query={name:_text(values.get(name)) for name in PACKING_FILTERS}
    query['print_state']=query['print_state'] or 'all'
    query['sort']=query['sort'] or 'default'
    query['status']=query['status'] or 'all'
    if query['print_state'] not in {'all','unprinted','reserved','uncertain','printed'}:
        raise ValueError('请选择有效打印状态')
    if query['sort'] not in {'default','location','order_date','delivery_date','receipt_date'}:
        raise ValueError('请选择有效排序方式')
    for start,end,label in (('order_date_from','order_date_to','订单日期'),('due_from','due_to','交期'),('receipt_from','receipt_to','入库日期')):
        for field in (start,end):
            if query[field] and (_day(query[field])!=query[field]):raise ValueError(label+'必须为 YYYY-MM-DD')
        if query[start] and query[end] and query[start]>query[end]:raise ValueError(label+'开始日期不能晚于结束日期')
    query['order_no_match']=query['order_no_match'] or 'exact'
    if query['order_no_match'] not in {'exact','contains'}:raise ValueError('单号匹配方式无效')
    return query


def _phase_from_id(identity):
    parts = _text(identity).split(':',2)
    return parts[1] if len(parts)==3 and parts[0]=='phase' and parts[1] in {'todo','in_progress','exception'} else ''


def _num(value):
    number = _decimal(value)
    return format(number.normalize(), 'f') if number else '0'


def _identity(row):
    return tuple(_text(row.get(k)) for k in ('profile_id','warehouse_id','product_id','sku_id','base_unit'))


def _display_unit(row, inventory=None):
    # Identity codes are not human unit names (ERP commonly uses base_unit="1").
    for source in (row, inventory or {}):
        for field in ('unit_name','base_unit_name','basic_unit_name','unit','base_unit'):
            value=_text(source.get(field))
            if value and not value.isdigit():
                return value
    return ''


class TaskBoard:
    def __init__(self, store, daily_work, customer_orders):
        self.store, self.daily, self.customers = store, daily_work, customer_orders
        self.purchase = None
        self.history_archive = None
        from .packing_index import PackingIndex
        self._packing_index = PackingIndex(store,customer_orders)
        daily_work.packing_index=self._packing_index
        self._materialization = BoardMaterialization(store)

    @staticmethod
    def _authorize(user, role, view):
        if role not in ROLES or view not in {'mine','role'}:
            raise ValueError('请选择有效岗位和任务范围')
        if not _is_admin(user) and role not in _roles(user):
            raise DailyWorkConflict('账号没有该岗位权限', 'role_forbidden')

    def _meta(self, db):
        today = self.daily.today()
        version = _text(_setting(db,'data_version'))
        updated = _text(_setting(db,'data_source_updated_at'))
        settings = {row[0]: row[1] for row in db.execute('SELECT key,value FROM daily_work_settings')}
        return {'business_date': today,'data_version': version,
            'print_state_version':_print_state_version(db),
            'task_version': int(settings.get('task_version','0')),
            'source_updated_at': updated,'task_data_version': settings.get('projection_data_version',''),
            'tasks_current': bool(version and version == settings.get('projection_data_version') and
                updated and updated == settings.get('projection_source_updated_at')),
            'stale': not _source_is_today(updated,today),
            'assignment_enabled': settings.get('assignment_enabled') == 'true'}

    @staticmethod
    def _assert_version(meta, data_version='', task_version=None, print_state_version=''):
        if data_version and data_version != meta['data_version']:
            raise DailyWorkConflict('业务数据版本已变化，请重新加载第一页', 'data_version_changed')
        if task_version not in (None,'') and int(task_version) != meta['task_version']:
            raise DailyWorkConflict('工作清单已变化，请重新加载第一页', 'task_version_changed')
        if print_state_version and print_state_version != meta['print_state_version']:
            raise DailyWorkConflict('打印状态已变化，请重新加载第一页并保留原选择范围', 'print_state_version_changed')

    def _events(self, db, user, role, view):
        actor, today = _user_id(user), self.daily.today()
        params = [today,role]
        sql = """SELECT e.*,t.role,t.title,t.order_no FROM daily_work_events e
            JOIN daily_work_tasks t ON e.task_id=t.id WHERE substr(e.created_at,1,10)=? AND t.role=?
            AND e.event_type IN ('start','progress','wait','resolve','packed','reopen','erp_business_completed')"""
        if view == 'mine':
            sql += " AND (e.actor_id=? OR (e.event_type='erp_business_completed' AND t.assignee_id=?))"
            params.extend([actor,actor])
        events = [{key: row[key] for key in ('id','task_id','event_type','actor_id','role','title','note','created_at')}
                  for row in db.execute(sql,params)]
        if role == 'sales':
            params = [today]
            sql = """SELECT e.*,f.customer_order_no FROM customer_order_followup_events e
                JOIN customer_order_followups f ON e.group_id=f.group_id WHERE substr(e.created_at,1,10)=?
                AND e.waiting_for NOT LIKE '负责人转交:%'
                AND e.waiting_for NOT LIKE '客户固定负责人变更:%'"""
            if view == 'mine':
                sql += ' AND e.actor_id=?';params.append(actor)
            events += [{'id':row['id'],'task_id':row['group_id'],'event_type':'followup',
                'actor_id':row['actor_id'],'role':'sales','title':row['customer_order_no'],
                'note':row['note'],'created_at':row['created_at']} for row in db.execute(sql,params)]
            sql="SELECT * FROM customer_order_work_actions WHERE substr(created_at,1,10)=?"
            params=[today]
            if view=='mine':sql+=' AND actor_id=?';params.append(actor)
            events += [{'id':row['id'],'task_id':row['group_id'],'event_type':row['action'],
                'actor_id':row['actor_id'],'role':'sales','title':'销售处理',
                'note':row['note'],'created_at':row['created_at']} for row in db.execute(sql,params)]
        return sorted(events,key=lambda row:(row['created_at'],row['id']),reverse=True)

    @staticmethod
    def _work_state(task, print_state='unprinted'):
        return 'in_progress' if task.get('_manual_work_started') or print_state in {'reserved','uncertain','printed'} or task['status'] in {
            'in_progress','waiting','packed','resolved'} else 'todo'

    @staticmethod
    def _manual_work_started(db, identity):
        row=db.execute("SELECT event_type FROM daily_work_events WHERE task_id=? AND actor_id<>'' AND actor_id NOT LIKE 'system:%' AND event_type IN ('start','progress','wait','resolve','packed','reopen') ORDER BY rowid DESC LIMIT 1",(identity,)).fetchone()
        return bool(row and row['event_type']!='reopen')

    def _sales_work_state(self, db, group, facts=None):
        if facts is not None:
            explicit=facts['sales_actions'].get(group['id'])
            if explicit:
                return 'in_progress' if explicit=='start' else 'todo'
            if group['follow_up_status'] in {'in_progress','waiting','resolved','packed'}:
                return 'in_progress'
            return 'in_progress' if any(facts['sales_started'].get((group['profile_id'],line['order_no']))
                for line in group['lines']) else 'todo'
        explicit = db.execute("SELECT action FROM customer_order_work_actions WHERE group_id=? AND action IN ('start','reset_to_todo') ORDER BY rowid DESC LIMIT 1",(group['id'],)).fetchone()
        if explicit:
            return 'in_progress' if explicit['action']=='start' else 'todo'
        if group['follow_up_status'] in {'in_progress','waiting','resolved','packed'}:
            return 'in_progress'
        return 'in_progress' if any(self._manual_work_started(db,task['id']) or
            task['status'] in {'in_progress','waiting','resolved','packed'}
            for line in group['lines'] for task in db.execute("SELECT id,status FROM daily_work_tasks WHERE profile_id=? AND order_no=? AND role='sales' AND status<>'business_completed'",
                (group['profile_id'],line['order_no']))) else 'todo'

    @staticmethod
    def _sales_notes(db, identity, facts=None):
        if facts is not None:
            return facts['notes'].get(identity,[])
        notes=[{**dict(row),'event_type':'followup','_source':1} for row in db.execute('''SELECT note,actor_id,created_at,rowid AS _rowid FROM customer_order_followup_events
            WHERE group_id=? AND trim(note)<>'' AND actor_id<>'' AND actor_id NOT LIKE 'system:%'
            AND waiting_for NOT LIKE '负责人转交:%' AND waiting_for NOT LIKE '客户固定负责人变更:%' ''',(identity,))]
        notes += [{**dict(row),'event_type':'note','_source':2} for row in db.execute("SELECT note,actor_id,created_at,rowid AS _rowid FROM customer_order_work_actions WHERE group_id=? AND action='note' AND trim(note)<>''",(identity,))]
        return [{key:value for key,value in row.items() if not key.startswith('_')} for row in
            sorted(notes,key=lambda row:(row['created_at'],row['_source'],row['_rowid']),reverse=True)[:100]]

    def _board_groups(self, db, groups):
        """Current boards use published unfulfilled groups, never saved history."""
        return groups

    def _shared_inputs(self, db, generation=None):
        def build():
            staff={row['id']:dict(row) for row in db.execute('SELECT * FROM staff_users')}
            inventory={}
            for row in db.execute("SELECT payload FROM records WHERE kind='inventory'"):
                item=_json(row[0],{})
                inventory[_identity(item)]=item
            source=_published_order_groups(db, current_only=True)
            groups=self.customers._build(db)[0]
            by_order=defaultdict(list)
            for group in groups.values():
                for line in group['lines']:
                    by_order[(group['profile_id'],line['order_no'])].append(group)
            manual={}
            for row in db.execute("""SELECT task_id,event_type FROM daily_work_events
                WHERE actor_id<>'' AND actor_id NOT LIKE 'system:%'
                AND event_type IN ('start','progress','wait','resolve','packed','reopen') ORDER BY rowid"""):
                manual[row['task_id']]=row['event_type']!='reopen'
            tasks=defaultdict(list)
            sales_started={}
            for row in db.execute("""SELECT * FROM daily_work_tasks
                WHERE condition_state='active' OR (role='sales' AND status<>'business_completed')"""):
                task=dict(row)
                task['_manual_work_started']=manual.get(task['id'],False)
                if task['role']=='sales' and task['status']!='business_completed':
                    ref=(task['profile_id'],task['order_no'])
                    sales_started[ref]=sales_started.get(ref,False) or task['_manual_work_started'] or task['status'] in {'in_progress','waiting','resolved','packed'}
                if task['condition_state']=='active' and _current_task(task, source):
                    task['payload']=_json(task['payload'],{})
                    tasks[task['role']].append(task)
            actions={}
            for row in db.execute("SELECT group_id,action FROM customer_order_work_actions WHERE action IN ('start','reset_to_todo') ORDER BY rowid"):
                actions[row['group_id']]=row['action']
            notes=defaultdict(list)
            for row in db.execute("""SELECT group_id,note,actor_id,created_at,rowid AS _rowid FROM customer_order_followup_events
                WHERE trim(note)<>'' AND actor_id<>'' AND actor_id NOT LIKE 'system:%'
                AND waiting_for NOT LIKE '负责人转交:%' AND waiting_for NOT LIKE '客户固定负责人变更:%'"""):
                notes[row['group_id']].append({**dict(row),'event_type':'followup','_source':1})
            for row in db.execute("SELECT group_id,note,actor_id,created_at,rowid AS _rowid FROM customer_order_work_actions WHERE action='note' AND trim(note)<>''"):
                notes[row['group_id']].append({**dict(row),'event_type':'note','_source':2})
            notes={identity:[{key:value for key,value in row.items() if not key.startswith('_') and key!='group_id'}
                for row in sorted(values,key=lambda row:(row['created_at'],row['_source'],row['_rowid']),reverse=True)[:100]]
                for identity,values in notes.items()}
            latest={}
            for row in db.execute('SELECT group_id,note,waiting_for FROM customer_order_followup_events ORDER BY created_at,id'):
                latest[row['group_id']]=dict(row)
            print_states={}
            print_details={}
            print_reasons={};print_audits={}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_states'").fetchone():
                for row in db.execute('SELECT profile_id,order_no,state,reason FROM order_print_states'):
                    ref=(row['profile_id'],row['order_no'])
                    print_states[ref]=row['state'];print_reasons[ref]=row['reason']
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_audit'").fetchone():
                for row in db.execute('SELECT profile_id,order_no,from_state,to_state,reason FROM order_print_audit ORDER BY rowid'):
                    print_audits[(row['profile_id'],row['order_no'])]=dict(row)
            from .order_print_state import print_state_details
            refs={(task['profile_id'],task['order_no']) for role in PACKING_ROLES for task in tasks[role]}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_states'").fetchone():
                print_details=print_state_details(db,refs)
            return {'staff':staff,'source':source,'inventory':inventory,
                'groups':groups,'groups_by_order':by_order,'tasks':tasks,'print_states':print_states,
                'print_details':print_details,
                'print_reasons':print_reasons,'print_audits':print_audits,
                'followups':{row['object_id']:dict(row) for row in db.execute('SELECT * FROM task_board_followups')},
                'sales_actions':actions,'sales_started':sales_started,'notes':notes,'latest_followups':latest}
        return self._materialization.get(generation,('inputs',self.daily.today(),datetime.now(SHANGHAI).date().isoformat()),build)

    def _packing_inputs(self,db,user,role,view,query):
        if db.execute('SELECT dirty FROM packing_index_meta WHERE id=1').fetchone()[0]:
            return self._shared_inputs(db,self._materialization.generation(db))
        source,inventory,groups,by_order,tasks=self._packing_index.inputs(db,role,_user_id(user),view,query)
        from .order_print_state import print_state_details
        details=print_state_details(db,set(source)) if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_states'").fetchone() else {}
        audits={}
        for ref,fact in details.items():
            if fact.get('reason')=='content_changed':
                row=db.execute('SELECT from_state,to_state,reason FROM order_print_audit WHERE profile_id=? AND order_no=? ORDER BY rowid DESC LIMIT 1',ref).fetchone()
                if row:audits[ref]=dict(row)
        return {'staff':{row['id']:dict(row) for row in db.execute('SELECT * FROM staff_users')},
            'source':source,'inventory':inventory,'groups':groups,'groups_by_order':by_order,'tasks':tasks,
            'print_details':details,'print_states':{r:f['state'] for r,f in details.items()},
            'print_reasons':{r:f.get('reason','') for r,f in details.items()},'print_audits':audits,
            'followups':{row['object_id']:dict(row) for row in db.execute('SELECT * FROM task_board_followups')},
            'sales_actions':{},'sales_started':{},'notes':{},'latest_followups':{}}

    def _prepare_views(self, packing=False):
        if packing and self.store._reader.get() is None:self._packing_index.refresh()
        self._materialization.prepare()

    def _materialized(self, db, user, role, view, phased=False, packing_query=None):
        self._authorize(user,role,view)
        generation=self._materialization.generation(db)
        key=('board',self.daily.today(),datetime.now(SHANGHAI).date().isoformat(),
             _user_id(user),_is_admin(user),tuple(sorted(_roles(user))),role,view,bool(phased),
             tuple(sorted(packing_query.items())) if packing_query is not None else None)
        def build():
            cards,meta=self._build_uncached(db,user,role,view,phased,generation,packing_query)
            if packing_query is not None:
                cards=self._filtered(cards,status=packing_query['status'])
                sorting=packing_query['sort']
                if sorting!='default':
                    field={'location':'location','order_date':'order_date','delivery_date':'due_date','receipt_date':'receipt_date'}[sorting]
                    cards=sorted(cards,key=lambda card:((natural_location_key(card.get(field,'')) if field=='location' else card.get(field,'') or '9999-12-31'),card['id']))
            return {'cards':cards,'meta':meta,'by_id':{card['id']:card for card in cards},
                    'events':[]}
        return self._materialization.get(generation,key,build)

    def _build(self, db, user, role, view, phased=False, packing_query=None):
        self._authorize(user,role,view)
        if self.store._reader.get() is not db:
            cards,meta=self._build_uncached(db,user,role,view,phased,packing_query=packing_query)
            if packing_query is not None:cards=self._filtered(cards,status=packing_query['status'])
            return cards,meta
        result=self._materialized(db,user,role,view,phased,packing_query)
        return result['cards'],result['meta']

    def _build_uncached(self, db, user, role, view, phased=False, generation=None, packing_query=None):
        meta = self._meta(db)
        actor = _user_id(user)
        facts=(self._packing_inputs(db,user,role,view,packing_query) if packing_query is not None
               else self._shared_inputs(db,generation))
        staff=facts['staff']
        options = [{'id':uid,'display_name':row['display_name']} for uid,row in staff.items()
                   if role in _json(row['roles'],[]) and row['active']]
        source,inventory,groups=facts['source'],facts['inventory'],facts['groups']
        if phased and role=='sales':
            groups=self._board_groups(db,groups)
        purchase_payloads={task['id']:task['payload'] for task in facts['tasks']['purchase']}
        followups,print_states,tasks=facts['followups'],facts['print_states'],facts['tasks'][role]
        print_mode=packing_query is not None
        if print_mode and role in PACKING_ROLES:
            # A shortage broadcast can coexist with a prior physical packing
            # task kept for audit. It is one order, not twice the quantity.
            actual={(t['profile_id'],t['order_no']):t for t in tasks if t['task_type']=='whole_order_pack'}
            broadcasts={(t['profile_id'],t['order_no']):t for t in tasks if t['task_type']=='arrival_shortage_waiting'}
            merged=[]
            for task in tasks:
                ref=(task['profile_id'],task['order_no'])
                if task['task_type']=='arrival_shortage_waiting' and ref in actual:continue
                if task['task_type']=='whole_order_pack' and ref in broadcasts:
                    fresh=broadcasts[ref]['payload']
                    payload={**task['payload'],**{k:v for k,v in fresh.items() if k.startswith('arrival_')}}
                    payload.update(read_only=True,blocked_by=fresh.get('blocked_by') or '整单未齐货，暂不能打印')
                    task={**task,'payload':payload}
                merged.append(task)
            tasks=merged
        def changed_label_phase(task):
            ref=(task['profile_id'],task['order_no'])
            audit=facts['print_audits'].get(ref,{})
            payload=task['payload']
            return bool(print_mode and print_states.get(ref)=='unprinted' and facts['print_reasons'].get(ref)=='content_changed' and
                any(_flag(payload.get(field)) for field in ('label_review_required','source_missing_review','source_scope_review')) and
                audit.get('reason')=='content_changed' and audit.get('to_state')=='unprinted' and audit.get('from_state') in {'printed','uncertain'})
        def packing_phase(task,state):
            # Source anomalies keep the durable print fact. Human start/wait/
            # packed events remain history and do not move this new view.
            return ('in_progress' if state in {'reserved','uncertain','printed'} or changed_label_phase(task) else
                    'exception' if role=='arrival_packing' and task['source_category']=='异常' and not task['payload'].get('arrival_partial') else 'todo') if print_mode else self._work_state(task,state)
        def order_fields(task,rows):
            matches=facts['groups_by_order'].get((task['profile_id'],task['order_no']),[])
            customers={_customer(row) for row in rows if _customer(row)} or {_text(task['customer'])}
            pos={_text(group['customer_order_no']) for group in matches if _text(group['customer_order_no'])}
            pos.update(_text(row.get('customer_order_no')) for row in rows if _text(row.get('customer_order_no')))
            for row in rows:
                note=parse_note(row.get('summary') or row.get('system_note'))
                # The source already binds this shared summary to the order.
                # Its label SKU/quantity never establish line identity.
                if note:
                    po=_text(note.get('orderNo'))
                    if po and po!='-':pos.add(po)
            dates={_day(row.get('order_date') or row.get('document_date')) for row in rows}
            dates.discard('')
            order_date=min(dates,default=_day(task['payload'].get('order_date')))
            return customers,pos,order_date
        def order_matches(task,rows):
            q=packing_query
            customers,pos,order_date=order_fields(task,rows)
            if q['customer_key'] and q['customer_key'] not in {_hash([task['profile_id'],c]) for c in customers}:return False
            if q['customer'] and q['customer'] not in customers:return False
            if q['sku'] and q['sku'] not in ({_text(row.get('sku')) for row in rows} or {_text(task['sku'])}):return False
            if q['order_no']:
                values={task['order_no'],*pos}
                if q['order_no_match']=='contains':
                    if not any(q['order_no'].casefold() in value.casefold() for value in values):return False
                elif q['order_no'] not in values:return False
            receipts=task['payload'].get('arrival_sources',[])
            if q['supplier'] or q['receipt_from'] or q['receipt_to']:
                if not any((not q['supplier'] or r.get('supplier_key')==q['supplier']) and
                    (not q['receipt_from'] or r.get('receipt_date','')>=q['receipt_from']) and
                    (not q['receipt_to'] or bool(r.get('receipt_date')) and r['receipt_date']<=q['receipt_to']) for r in receipts):return False
            if q['assignee_id'] and task['assignee_id']!=q['assignee_id']:return False
            if q['print_state']!='all' and print_states.get((task['profile_id'],task['order_no']),'unprinted')!=q['print_state']:return False
            for value,start,end in ((order_date,'order_date_from','order_date_to'),(task['due_date'],'due_from','due_to')):
                if q[start] and (not value or value<q[start]):return False
                if q[end] and (not value or value>q[end]):return False
            if q['keyword']:
                terms=[task['order_no'],task['location'],task['sku'],*customers,*pos]
                terms.extend(_text(row.get(field)) for row in rows for field in ('sku','product_name','specification','spec','model','summary','system_note','location'))
                if q['keyword'].casefold() not in ' '.join(terms).casefold():return False
            return True
        def staff_name(uid):
            return _text(staff.get(uid,{}).get('display_name'))
        def editable(task):
            payload = task['payload']
            return bool((_is_admin(user) or task['assignee_id'] == actor) and task['assignee_id'] and
                        not _flag(payload.get('read_only')) and task['status'] in {'todo','in_progress','waiting'})
        def task_line(task):
            rows = source.get((task['profile_id'],task['order_no']),[])
            first = rows[0] if rows else {}
            units = {_text(row.get('base_unit')) for row in rows}
            quantity = _num(sum((_decimal(row.get('quantity')) for row in rows),Decimal(0))) if len(units)==1 else ''
            safety = _text(task['payload'].get('blocked_by'))
            state = print_states.get((task['profile_id'],task['order_no']),'unprinted')
            print_fact=facts['print_details'].get((task['profile_id'],task['order_no']),{})
            print_safety=safety
            if role in PACKING_ROLES:
                safety = safety or _lifecycle_review_reason(rows)
                if not rows or any(not all(_identity(row)) for row in rows):
                    safety = safety or '商品身份待核对'
                if state in {'reserved','uncertain'}:
                    safety = safety or '打印进行中或出纸待核对'
                if role=='stock_packing' and any(group['customer_review'] for group in
                        facts['groups_by_order'].get((task['profile_id'],task['order_no']),[])):
                    safety=safety or '客户身份待核对'
                persisted_block=_text(task['payload'].get('blocked_by'))
                if state=='uncertain' and persisted_block==PRINT_REVIEW_BLOCK:persisted_block=''
                print_safety=(persisted_block or _lifecycle_review_reason(rows) or
                    ('商品身份待核对' if not rows or any(not all(_identity(row)) for row in rows) else '') or
                    ('客户身份待核对' if role=='stock_packing' and any(group['customer_review'] for group in facts['groups_by_order'].get((task['profile_id'],task['order_no']),[])) else '') or
                    ('旧打印批次已撤销，请核对实际出纸' if print_fact.get('print_job_revoked') else f"订单 {task['order_no']} 正由 {print_fact.get('current_print_actor_name') or _text(print_fact.get('device_id')) or '另一台设备'} 打印，不能重复占用" if state=='reserved' else ''))
            customers,pos,order_date=order_fields(task,rows)
            can_print=bool(role in PACKING_ROLES and (_is_admin(user) or task['assignee_id']==actor) and task['assignee_id'] and
                staff.get(task['assignee_id'],{}).get('active') and role in _json(staff.get(task['assignee_id'],{}).get('roles'),[]) and not _flag(task['payload'].get('read_only')) and
                task['condition_state']=='active' and task['status']!='business_completed' and not print_safety)
            return {'id':task['id'],'task_id':task['id'],'version':task['version'],
                'order_no':task['order_no'],'sku':_text(first.get('sku') or task['sku']),
                'product_name':_text(first.get('product_name')),'quantity':quantity or _text(task['payload'].get('quantity')),
                'unit':_display_unit(first,inventory.get(_identity(first),{})) if len(units)==1 else '',
                'unit_note':'基本单位未命名' if len(units)==1 and not _display_unit(first,inventory.get(_identity(first),{})) else '', 'location':task['location'],
                'summary':'\n'.join(dict.fromkeys(_text(row.get('summary') or row.get('system_note')) for row in rows)),
                'status':task['status'],'print_state':state,'work_state':packing_phase(task,state),'arrival_sources':task['payload'].get('arrival_sources',[]),
                'arrival_received':task['payload'].get('arrival_received',''),'arrival_required':task['payload'].get('arrival_required',''),
                'arrival_shortage':task['payload'].get('arrival_shortage',''),'arrival_partial':bool(task['payload'].get('arrival_partial')),
                'arrival_overdue':task['source_category']=='异常' or any((_day(r.get('receipt_date')) and (date.fromisoformat(self.daily.today())-date.fromisoformat(r['receipt_date'])).days>7) for r in task['payload'].get('arrival_sources',[])),'assignee_id':task['assignee_id'],
                'customer_order_nos':sorted(pos),'order_date':order_date,'can_print':can_print,'print_blocked_by':print_safety,
                'label_content_review':changed_label_phase(task),'print_job_revoked':bool(print_fact.get('print_job_revoked')),
                **facts['print_details'].get((task['profile_id'],task['order_no']),{}),
                'assignee_name':staff_name(task['assignee_id']),'can_act':editable(task) and not bool(safety),
                'executable':task['status'] in {'todo','in_progress','waiting'} and not bool(safety) and not _flag(task['payload'].get('read_only')), 
                'blocked_by':safety,'customer':task['customer'],'due_date':task['due_date'],
                'product_lines':[{'record_id':row.get('id',''),'sku':row.get('sku',''),
                    'quantity':_text(row.get('quantity')),'unit':_display_unit(row,inventory.get(_identity(row),{})),
                    'summary':row.get('summary') or row.get('system_note','')} for row in rows]}
        cards=[]
        if role == 'sales':
            for group in groups.values():
                if not group['unshipped_count'] and not (phased and group['customer_review']):
                    continue
                if view == 'mine' and actor not in {group['assignee_id'],group.get('deputy_id','')}:
                    continue
                identity = group['id']
                extra = followups.get(identity,{})
                lines = []
                for line in group['lines']:
                    originals = source.get((group['profile_id'],line['order_no']),[])
                    units = {_text(row.get('base_unit')) for row in originals}
                    lines.append({'id':line['order_no'],'task_id':'','version':0,'order_no':line['order_no'],
                        'sku':' / '.join(line['skus']),'product_name':' / '.join(dict.fromkeys(_text(row.get('product_name')) for row in originals)),
                        'quantity':_num(sum((_decimal(row.get('quantity')) for row in originals),Decimal(0))) if len(units)==1 else '',
                        'unit':_display_unit(originals[0],inventory.get(_identity(originals[0]),{})) if originals and len(units)==1 else '', 'location':' / '.join(dict.fromkeys(_text(row.get('location')) for row in originals)),
                        'summary':'\n'.join(dict.fromkeys(_text(row.get('summary') or row.get('system_note')) for row in originals)),
                        'status':line['status'],'print_state':line['print_state'],'can_act':False,
                        'fulfillment_label':line.get('fulfillment_label',''),
                        'packing_basis':line.get('packing_basis',''),
                        'assignee_id':group['assignee_id'],'assignee_name':staff_name(group['assignee_id']),
                        'blocked_by':'资料待核对' if line['status']=='review' else '',
                        'customer':group['customer'],'due_date':line['due_date']})
                notes=self._sales_notes(db,identity,facts)
                phase=self._sales_work_state(db,group,facts)
                card={'id':identity,'kind':'sales','role':role,'title':group['customer'] or '客户待核对',
                    'subtitle':group['customer_order_no'],'customer_order_no':group['customer_order_no'],
                    'customer':group['customer'],'due_date':group['due_date_from'],
                    'status':'waiting' if group['follow_up_status'] in {'waiting','in_progress'} else 'todo',
                    'work_state':phase,'work_label':'处理中' if phase=='in_progress' else '待处理',
                    'has_note':bool(notes),'latest_note':notes[0]['note'] if notes else '',
                    'business_status':group['status'],'blocked_by':('订单来源暂时缺失，等待 ERP 核对' if group.get('source_missing_review') else '归组资料待核对' if group['customer_review'] else
                        '客户固定销售待指定或核对' if group.get('owner_review') or not group['assignee_id'] else ''),
                    'assignee_id':group['assignee_id'],'assignee_name':staff_name(group['assignee_id']),
                    'can_act':bool(group['assignee_id'] and not group.get('source_missing_review') and not group['customer_review'] and not group.get('owner_review') and
                        (_is_admin(user) or actor in {group['assignee_id'],group.get('deputy_id','')})),
                    'urgent':group['urgent'],'ready_count':group['ready_count'],'missing_count':group['missing_count'],
                    'review_count':group['review_count'],'packed_count':group['packed_count'],
                    'total_count':group['unshipped_count'],'order_count':group['order_count'],
                    'next_follow_up_date':group['next_follow_up_date'],'version':group['follow_up_version'],
                    'missing_items':group['missing_items'],'lines_changed':group['lines_changed'],
                    'profile_id':group['profile_id'],'deputy_id':group.get('deputy_id',''),
                    'owner_review':bool(group.get('owner_review')),
                    '_lines':lines,'_refs':[{'kind':'sales','id':identity,'version':group['follow_up_version']}],
                    '_related':[],'_notes':notes, **{name:_text(extra.get(name)) for name in ('waiting_for','promised_date','result','note')}}
                if not extra:
                    event = facts['latest_followups'].get(identity)
                    if event:
                        card.update(waiting_for=event['waiting_for'],note=event['note'])
                purchases = [{**task,'assignee_name':staff_name(task['assignee_id']),
                    'promised_date':_text(purchase_payloads.get(task['id'],{}).get('promised_date')),
                    'waiting_for':_text(purchase_payloads.get(task['id'],{}).get('waiting_for'))} for task in group['purchase_tasks']]
                card['purchase_tasks'] = purchases
                known_promises=[task['promised_date'] for task in purchases if task['promised_date']]
                if not card['promised_date'] and purchases and len(known_promises)==len(purchases):
                    card['promised_date']=max(known_promises)
                card['promise_incomplete']=bool(purchases and len(known_promises)<len(purchases))
                if not card['waiting_for']:
                    card['waiting_for'] = '、'.join(dict.fromkeys(task['assignee_name'] for task in purchases if task['assignee_name']))
                cards.append(card)
        else:
            bins=defaultdict(list)
            for task in tasks:
                if view=='mine' and task['assignee_id'] != actor and not (print_mode and role=='arrival_packing' and task['task_type']=='arrival_shortage_waiting' and not task['assignee_id']):
                    continue
                rows=source.get((task['profile_id'],task['order_no']),[])
                if print_mode and not order_matches(task,rows):continue
                if role in PACKING_ROLES:
                    identities=sorted({_identity(row) for row in rows})
                    if phased and role in PACKING_ROLES and not rows:
                        known=task['payload'].get('product_identities',[])
                        if len(known)==1:
                            parts=tuple(_text(known[0]).split('|'))
                            if len(parts)==5 and all(parts):identities=[parts]
                    if len(identities)==1 and all(identities[0]):
                        key=('packing',role,*identities[0])
                    else:
                        key=('combination',role,task['profile_id'],task['order_no'])
                else:
                    identity=purchase_identity(task,source)
                    key=('purchase',role,identity)
                if phased and role in PACKING_ROLES:
                    key=(*key,packing_phase(task,print_states.get((task['profile_id'],task['order_no']),'unprinted')))
                bins[key].append(task)
            for key,members in bins.items():
                identity=('phase:'+key[-1]+':'+_hash([key[:-1],sorted(task['id'] for task in members)])[:32]) if phased and role in PACKING_ROLES else _hash(key)[:32]
                lines=[task_line(task) for task in members]
                order_refs=set()
                for task in members:
                    order_refs.update((task['profile_id'],number) for number in task['payload'].get('order_nos', [task['order_no']]) if number and (task['profile_id'],number) in source)
                originals=[row for ref in order_refs for row in source.get(ref,[])]
                if role=='purchase':
                    originals=[row for row in originals if _record_identity(row)==key[-1]]
                first=originals[0] if originals else {}
                matching_identity=_identity(first) if first else None
                inv=inventory.get(matching_identity,{})
                owners=sorted({task['assignee_id'] for task in members if task['assignee_id']})
                owner=owners[0] if len(owners)==1 else ''
                due=min((task['due_date'] for task in members if task['due_date']),default='')
                location=min((task['location'] for task in members if task['location']),key=natural_location_key,default='')
                statuses={line['status'] for line in lines}
                status='todo' if 'todo' in statuses else 'in_progress' if 'in_progress' in statuses else 'waiting' if 'waiting' in statuses else 'packed' if 'packed' in statuses else 'resolved'
                related=[]
                for profile,number in sorted(order_refs):
                    matches=facts['groups_by_order'].get((profile,number),[])
                    for group in matches:
                        if not any(item['id']==group['id'] for item in related):
                            related.append({'id':group['id'],'customer_order_no':group['customer_order_no'],
                                'customer':group['customer'],'assignee_id':group['assignee_id'],
                                'assignee_name':staff_name(group['assignee_id']),
                                'missing_count':group['missing_count'],'due_date':group['due_date_from'],
                                'review_required':len(matches)>1 or bool(group.get('customer_review')) or
                                    bool(group.get('owner_review')) or not group['assignee_id']})
                shortage=pending=Decimal(0)
                near_complete=0
                if role=='purchase':
                    shortage,pending=purchase_totals(members,[groups[g['id']] for g in related],matching_identity)
                    for related_group in related:
                        group=groups[related_group['id']]
                        if group['missing_product_count']==1 and not group['review_count'] and not group['customer_review'] and any(
                                matching_identity and (missing['product_id'],missing['sku_id'],missing['base_unit'])==matching_identity[2:] for missing in group['missing_items']):
                            near_complete+=1
                        relevant=[missing for missing in group['missing_items'] if matching_identity and
                            (missing['product_id'],missing['sku_id'],missing['base_unit'])==matching_identity[2:]]
                        related_group.update(shortage=_num(sum((_decimal(row['shortage']) for row in relevant),Decimal(0))),
                            pending=_num(sum((_decimal(row['pending']) for row in relevant),Decimal(0))),
                            line_count=len(relevant))
                quantity = sum((_decimal(line['quantity']) for line in lines if line['executable'] or phased and role in PACKING_ROLES),Decimal(0)) if role in PACKING_ROLES and key[0]!='combination' else shortage+pending
                extra=followups.get(identity,{})
                if role=='purchase':
                    # A product card can outlive a completed demand cycle.
                    # Its old card-level note must not become a promise for
                    # a newly created procurement task of the same product.
                    newest=max(members,key=lambda task:task['updated_at'])
                    extra={**newest['payload'],'result':newest['payload'].get('followup_result','')}
                    promises={_text(task['payload'].get('promised_date')) for task in members}
                    if len(promises)!=1 or any(_flag(task['payload'].get('followup_source_changed')) for task in members):
                        extra['promised_date']=''
                elif not extra:
                    newest=max(members,key=lambda task:task['updated_at'])
                    extra=newest['payload']
                if role=='purchase' and originals:
                    # Procurement is acted on once per product; detail shows the
                    # affected actual order lines, never an empty synthetic order.
                    source_members={number:task for task in members for number in task['payload'].get('order_nos',[task['order_no']])}
                    expanded=[]
                    for original in sorted(originals,key=lambda row:(row['order_no'],row.get('id',''))):
                        task=source_members.get(original['order_no'],members[0])
                        line=task_line(task)
                        line.update(id=original['id'],order_no=original['order_no'],sku=original.get('sku',''),
                            product_name=original.get('product_name',''),quantity=_text(original.get('quantity')),
                            unit=_display_unit(original,inventory.get(_identity(original),{})),
                            summary=_text(original.get('summary') or original.get('system_note')),
                            due_date=_day(original.get('delivery_date')) or task['due_date'],
                            customer=_customer(original),
                            location=_text(original.get('location') or task['location']))
                        expanded.append(line)
                    lines=expanded
                card={'id':identity,'kind':key[0],'role':role,'title':_text(first.get('sku')) or ('组合包装' if key[0]=='combination' else members[0]['title']),
                    'subtitle':_text(first.get('product_name')),'sku':_text(first.get('sku') or members[0]['sku']),
                    'product_name':_text(first.get('product_name') or inv.get('product_name')),'specification':_text(first.get('specification') or first.get('spec') or inv.get('specification') or inv.get('spec') or first.get('model') or inv.get('model')),
                    'model':_text(first.get('model') or inv.get('model')) if key[0]!='combination' else '',
                    'receipt_date':min((r.get('receipt_date','') for line in lines for r in line.get('arrival_sources',[]) if r.get('receipt_date')),default=''),
                    'arrival_sources':list({r.get('event_id',dumps(r)):r for line in lines for r in line.get('arrival_sources',[])}.values()),
                    'arrival_received':_num(sum((_decimal(line.get('arrival_received')) for line in lines),Decimal(0))),
                    'arrival_required':_num(sum((_decimal(line.get('arrival_required')) for line in lines),Decimal(0))),
                    'arrival_shortage':_num(sum((_decimal(line.get('arrival_shortage')) for line in lines),Decimal(0))),
                    'arrival_partial':any(line.get('arrival_partial') for line in lines),
                    'arrival_overdue':any(line.get('arrival_overdue') for line in lines),
                    'unit':_display_unit(first,inv) if key[0]!='combination' else '',
                    'unit_note':'基本单位未命名' if key[0]!='combination' and not _display_unit(first,inv) else '',
                    'location':location,'quantity':_num(quantity) if key[0]!='combination' else '',
                    'shortage':_num(shortage),'pending':_num(pending),'order_count':len(order_refs),
                    'line_count':len(originals),
                    'due_date':due,'status':status,'blocked_by':next((line['blocked_by'] for line in lines if line['blocked_by']),''),
                    'order_date':min((line['order_date'] for line in lines if line.get('order_date')),default=''),
                    'can_print':any(line.get('can_print') for line in lines),
                    'assignee_id':owner,'assignee_name':staff_name(owner),'owner_names':[staff_name(uid) for uid in owners],
                    'can_act':any(line['can_act'] for line in lines),'urgent':any(task['urgent'] for task in members),
                    'lines_changed':any(_flag(task['payload'].get('followup_source_changed')) for task in members),
                    'customer_order_impact':len(related),'near_complete_customer_orders':near_complete,
                    'next_follow_up_date':min((task['next_follow_up_date'] for task in members if task['next_follow_up_date']),default=''),
                    'version':max(task['version'] for task in members),'profile_id':members[0]['profile_id'],
                    '_lines':lines,'_refs':[{'kind':'task','id':task['id'],'version':task['version']} for task in members],
                    '_related':related,**{name:_text(extra.get(name)) for name in ('waiting_for','promised_date','result','note')}}
                cards.append(card)
        for card in cards:
            if role!='sales':
                states={line['print_state'] for line in card['_lines']}
                state=next((value for value in ('uncertain','reserved','printed') if value in states),'unprinted')
                card['print_state']=state
                phase=next((phase for phase in ('in_progress','exception') if any(line['work_state']==phase for line in card['_lines'])),'todo')
                card['work_state']=phase
                card['work_label']={'uncertain':'打印待核对','reserved':'打印中','printed':'待出库'}.get(state,'处理中' if phase=='in_progress' else '异常' if phase=='exception' else '待处理')
                if print_mode and state=='printed':card['work_label']='已打印 · 待出库'
                if print_mode and any(line.get('print_job_revoked') for line in card['_lines']):card['work_label']='旧批次待核对'
                if print_mode and any(line.get('label_content_review') for line in card['_lines']):card['work_label']='标签内容变化待核对'
                card['has_note']=bool(card.get('note'))
                card['latest_note']=card.get('note','')
                latest=max(card['_lines'],key=lambda line:line.get('latest_print_at',''),default={})
                for field in ('latest_print_actor_id','latest_print_actor_name','latest_print_at'):
                    card[field]=latest.get(field,'')
                card['latest_print_actor_names']=sorted({line.get('latest_print_actor_name') for line in card['_lines'] if line.get('latest_print_actor_name')})
            card.setdefault('owner_names',[card['assignee_name']] if card.get('assignee_name') else [])
            card.setdefault('order_count',0)
            related = ([{'id':card['id'],'customer':card['customer'],
                         'assignee_id':card['assignee_id'],'assignee_name':card['assignee_name'],
                         'review_required':bool(card.get('owner_review')) or not card['assignee_id']}]
                       if role == 'sales' else card['_related'])
            card['customer_sales_owners'] = list({(item['customer'],item.get('assignee_id','')):{
                'customer':item['customer'],'owner_id':item.get('assignee_id',''),
                'owner_name':item['assignee_name'],'review_required':item['review_required']}
                for item in related}.values())
        meta['filter_options']={'assignees':options}
        meta['assignees']=options
        return cards,meta

    def _filtered(self, cards, keyword='', status='all', due_from='', due_to='', assignee_id=''):
        if status not in {'all','todo','in_progress','waiting','packed','review','shortage','packing','ready_to_ship','procurement','arrival'}:
            raise ValueError('筛选状态无效')
        for raw in (due_from,due_to):
            if raw and not _day(raw):
                raise ValueError('交期必须为 YYYY-MM-DD')
        if due_from and due_to and due_from > due_to:
            raise ValueError('交期开始日期不能晚于结束日期')
        needle=_text(keyword).casefold()
        result=[]
        for card in cards:
            if needle and needle not in ' '.join(_text(card.get(key)) for key in ('title','subtitle','sku','customer','customer_order_no','location')).casefold() and not any(needle in line['order_no'].casefold() for line in card['_lines']):
                continue
            if status!='all':
                if status in {'procurement','arrival'}:
                    matches=needs_procurement(card) if status=='procurement' else _decimal(card.get('pending'))>0
                elif status=='review':
                    matches=bool(card.get('blocked_by') or card.get('review_count'))
                elif status in {'shortage','packing','ready_to_ship'}:
                    matches=card.get('business_status')==status
                else:
                    matches=card['status']==status
                if not matches: continue
            if due_from and (not card['due_date'] or card['due_date']<due_from):continue
            if due_to and (not card['due_date'] or card['due_date']>due_to):continue
            if assignee_id and not (card.get('deputy_id')==assignee_id or
                                    any(line.get('assignee_id')==assignee_id for line in card['_lines'])):continue
            result.append(card)
        today=self.daily.today()
        soon=(date.fromisoformat(today)+timedelta(days=3)).isoformat()
        def key(card):
            due=card['due_date']
            risk=0 if card['urgent'] else 1 if due and due<=today else 2 if due and due<=soon else 3
            promised=card.get('promised_date','')
            if promised and promised<today:risk=min(risk,1)
            deferred=card['status']=='waiting' and card.get('next_follow_up_date','')>today and risk==3 and not card.get('lines_changed')
            return (bool(deferred),risk,due or '9999-12-31',card.get('missing_count',0),
                -(card.get('ready_count',0)/max(card.get('total_count',0),1)),
                -card.get('near_complete_customer_orders',0),natural_location_key(card.get('location','')),card['id'])
        return sorted(result,key=key)

    @staticmethod
    def _public(card):
        return deepcopy({key:value for key,value in card.items() if not key.startswith('_')})

    def list(self,user,role,view='mine',keyword='',status='all',page=1,data_version='',task_version=None,due_from='',due_to='',assignee_id='',work_state='',print_state_version='',packing_mode='',customer='',sku='',order_no='',order_date_from='',order_date_to='',print_state='all',sort='default',page_size=None,customer_key='',order_no_match='',supplier='',receipt_from='',receipt_to='',compact=False):
        if int(page)<1:raise ValueError('页码必须为正整数')
        if assignee_id and view!='role':raise ValueError('负责人筛选需要选择本岗位全部')
        if work_state and (work_state not in ({'todo','in_progress','exception'} if role=='arrival_packing' else {'todo','in_progress'}) or role not in TWO_STAGE_ROLES):
            raise ValueError('该岗位不支持此处理阶段')
        query=_packing_query(role,packing_mode,customer=customer,sku=sku,order_no=order_no,keyword=keyword,
            order_date_from=order_date_from,order_date_to=order_date_to,due_from=due_from,due_to=due_to,
            print_state=print_state,sort=sort,assignee_id=assignee_id,status=status,customer_key=customer_key,
            order_no_match=order_no_match,supplier=supplier,receipt_from=receipt_from,receipt_to=receipt_to)
        size=PAGE_SIZE if page_size in (None,'') else int(page_size)
        if query is None and size!=PAGE_SIZE:raise ValueError('自定义分页仅适用于打印视图')
        if not 1<=size<=100:raise ValueError('每页数量必须为 1 至 100')
        self._prepare_views(query is not None)
        with self.store.read_snapshot(), self.store.connect() as db:
            phased=bool(work_state or query is not None)
            cards,meta=self._build(db,user,role,view,phased=phased,packing_query=query)
            materialized=self._materialized(db,user,role,view,phased=phased,packing_query=query)
            meta=deepcopy(meta)
            self._assert_version(meta,data_version,task_version,print_state_version)
            if query is None:cards=self._filtered(cards,keyword,status,due_from,due_to,assignee_id)
            phase_counts={phase:sum(card['work_state']==phase for card in cards) for phase in (('todo','in_progress','exception') if role=='arrival_packing' else ('todo','in_progress'))}
            order_phase_counts={phase:sum(card['order_count'] for card in cards if card['work_state']==phase) for phase in (('todo','in_progress','exception') if role=='arrival_packing' else ('todo','in_progress'))}
            if work_state:cards=[card for card in cards if card['work_state']==work_state]
            events=[] if compact else deepcopy(self._events(db,user,role,view))
            summary={key:sum(card['status']==key for card in cards) for key in ('todo','waiting','packed','in_progress')}
            summary.update(total=len(cards),unassigned=sum(all(not line.get('assignee_id') for line in card['_lines']) for card in cards),
                actions_completed_today=sum(event['event_type']!='erp_business_completed' for event in events),
                business_completed_today=sum(event['event_type']=='erp_business_completed' for event in events),
                work_state_counts=phase_counts,order_work_state_counts=order_phase_counts)
            start=(int(page)-1)*size
            return {**meta,'items':[self._public(card) for card in cards[start:start+size]],
                'total':len(cards),'order_count':sum(card['order_count'] for card in cards),'packing_mode':packing_mode,
                'line_count':sum(card.get('line_count', 0) for card in cards),
                'page':int(page),'page_size':size,'has_more':start+size<len(cards),
                'summary':summary,'today_events':events}

    def filter_options(self,user,role,view='mine'):
        self._authorize(user,role,view)
        if role not in PACKING_ROLES:raise ValueError('请选择包装岗位')
        self._prepare_views(True)
        with self.store.read_snapshot(),self.store.connect() as db:
            cards,meta=self._build(db,user,role,view,phased=True,packing_query=_packing_query(role,'print'))
            customers={};suppliers={}
            for card in cards:
                for line in card['_lines']:
                    name=line.get('customer','')
                    if name:
                        key=_hash([card['profile_id'],name])
                        customers[key]={'id':key,'name':name,'profile_id':card['profile_id']}
                    for source in line.get('arrival_sources',[]):
                        key=source.get('supplier_key')
                        if key:suppliers[key]={'id':key,'name':source.get('supplier_name') or '供应商待核对'}
            return {**meta,'customers':sorted(customers.values(),key=lambda r:(r['name'],r['id'])),
                    'suppliers':sorted(suppliers.values(),key=lambda r:(r['name'],r['id']))}

    def detail(self,user,identity,role,view='mine',work_state='',print_state_version='',packing_mode='',**filters):
        if work_state and (work_state not in ({'todo','in_progress','exception'} if role=='arrival_packing' else {'todo','in_progress'}) or role not in TWO_STAGE_ROLES):
            raise ValueError('该岗位不支持此处理阶段')
        query=_packing_query(role,packing_mode,**filters)
        self._prepare_views(query is not None)
        with self.store.read_snapshot(), self.store.connect() as db:
            phased=bool(_phase_from_id(identity) or work_state or query is not None)
            _,meta=self._build(db,user,role,view,phased=phased,packing_query=query)
            materialized=self._materialized(db,user,role,view,phased=phased,packing_query=query)
            meta=deepcopy(meta)
            self._assert_version(meta,print_state_version=print_state_version)
            card=materialized['by_id'].get(identity)
            if not card or query is not None and work_state and card['work_state']!=work_state:raise KeyError('工作事项不存在或不在当前范围')
            events=self._events(db,user,role,view)
            refs={ref['id'] for ref in card['_refs']}
            return {**meta,'item':self._public(card),'lines':deepcopy(card['_lines']),
                'notes':deepcopy(card.get('_notes',[])),
                'related_groups':deepcopy(card['_related']),
                'events':deepcopy([event for event in events if event['task_id'] in refs])}

    def sales_action(self, user, identity, body):
        """Explicit sales phase and independent notes, with one durable replay result."""
        self._authorize(user,'sales','mine')
        action,key=_text(body.get('action')),_text(body.get('idempotency_key'))
        note=_text(body.get('note'))
        if action not in {'start','reset_to_todo','note'} or not key or len(key)>128:
            raise ValueError('请提供有效操作和幂等标识')
        if len(note)>1000 or action=='note' and not note or action!='note' and note:
            raise ValueError('备注操作需填写 1 至 1000 字说明，阶段操作不附备注')
        if not body.get('expected_data_version') or body.get('expected_task_version') is None:
            raise ValueError('操作需要当前数据及任务版本')
        try:expected=int(body.get('version'));int(body['expected_task_version'])
        except (TypeError,ValueError):raise ValueError('请提供当前跟进及任务版本') from None
        actor=_user_id(user)
        fingerprint=_hash({'group_id':identity,'body':body})
        with self.store.connect(write=True) as db:
            replay=db.execute('SELECT * FROM customer_order_work_actions WHERE actor_id=? AND idempotency_key=?',(actor,key)).fetchone()
            if replay:
                if replay['request_hash']!=fingerprint:
                    raise DailyWorkConflict('幂等标识已用于不同操作','idempotency_conflict')
                return {**_json(replay['result'],{}),'replayed':True}
            if _flag(_setting(db,'restoration_hold',False)):
                raise DailyWorkConflict('备份恢复后需核对工作记录，暂不能提交新操作','restoration_hold')
            meta=self._meta(db)
            self._assert_version(meta,body['expected_data_version'],body['expected_task_version'])
            if not meta['tasks_current']:
                raise DailyWorkConflict('任务尚未跟上已发布数据，请刷新','task_version_changed')
            group=self.customers._build(db)[0].get(identity)
            if not group:raise KeyError('客户订单不存在或来源暂时缺失')
            if self._board_groups(db,{identity:group}).get(identity,{}).get('source_missing_review'):
                raise DailyWorkConflict('订单来源暂时缺失，暂停提交并保留输入','task_source_review')
            if group['customer_review'] or group['owner_review']:
                raise DailyWorkConflict('客户身份或固定销售待核对，暂停操作','customer_identity_review')
            if not group['unshipped_count']:
                raise DailyWorkConflict('ERP 已确认该事项结束','task_condition_resolved')
            if not group['assignee_id'] or not _is_admin(user) and actor not in {group['assignee_id'],group.get('deputy_id','')}:
                raise DailyWorkConflict('客户未分配给当前销售','task_owner_conflict')
            if not _is_admin(user):
                staff=self.daily._staff(db,self.daily.today()).get(actor)
                if not staff or 'sales' not in staff['roles']:
                    raise DailyWorkConflict('账号当前未在岗或已失去销售权限','role_forbidden')
            row=db.execute('SELECT * FROM customer_order_followups WHERE group_id=?',(identity,)).fetchone()
            if expected!=(row['version'] if row else 0):
                raise DailyWorkConflict('跟进记录已更新，请刷新后重试','task_version_changed')
            prior_state=self._sales_work_state(db,group)
            state={'start':'in_progress','reset_to_todo':'todo'}.get(action,prior_state)
            status={'start':'in_progress','reset_to_todo':'todo'}.get(action,row['status'] if row else 'todo')
            now=timestamp()
            if row:
                db.execute('''UPDATE customer_order_followups SET status=?,version=version+1,
                    source_data_version=?,source_fingerprint=?,updated_at=? WHERE group_id=?''',
                    (status,meta['data_version'],group['source_fingerprint'],now,identity))
            else:
                db.execute('''INSERT INTO customer_order_followups
                    (group_id,profile_id,warehouse_id,customer_order_no,assignee_id,status,
                     next_follow_up_date,version,source_data_version,source_fingerprint,created_at,updated_at,customer_name)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identity,group['profile_id'],group['warehouse_id'],group['customer_order_no'],
                     group['assignee_id'],status,'',1,meta['data_version'],group['source_fingerprint'],now,now,group['customer']))
            notes=self._sales_notes(db,identity)
            response={'replayed':False,'version':expected+1,'task_version':self.daily._bump_version(db),
                'work_state':state,'has_note':bool(notes) or action=='note',
                'latest_note':note if action=='note' else notes[0]['note'] if notes else ''}
            db.execute('INSERT INTO customer_order_work_actions VALUES (?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,identity,actor,key,fingerprint,action,note,dumps(response),now))
            return response

    def export_snapshot(self,user,body):
        """Freeze complete visible order data for image sharing without writing task state."""
        role,view=_text(body.get('role')),_text(body.get('view')) or 'mine'
        identities=body.get('ids')
        if (not isinstance(identities,list) or not identities or len(identities)>500 or
                any(not isinstance(item,str) or not item for item in identities) or
                len(set(identities))!=len(identities)):
            raise ValueError('请选择 1 至 500 个不重复的工作事项')
        if not body.get('expected_data_version') or body.get('expected_task_version') is None:
            raise ValueError('导出需要当前数据及任务版本')
        query=_packing_query(role,body.get('packing_mode',''),**{key:body.get(key,'') for key in PACKING_FILTERS})
        if query is not None and not body.get('expected_print_state_version'):
            raise ValueError('导出需要当前打印状态版本')
        self._prepare_views(query is not None)
        with self.store.read_snapshot():
            with self.store.connect() as db:
                cards,meta=self._build(db,user,role,view,phased=bool(query is not None or body.get('work_state') or any(_phase_from_id(identity) for identity in identities)),packing_query=query)
                if body.get('work_state'):cards=[card for card in cards if card['work_state']==body['work_state']]
                self._assert_version(meta,body['expected_data_version'],body['expected_task_version'],body.get('expected_print_state_version',''))
                if not meta['tasks_current']:
                    raise DailyWorkConflict('任务尚未跟上已发布数据，请刷新','task_version_changed')
                visible={card['id']:card for card in cards}
                if any(identity not in visible for identity in identities):
                    raise DailyWorkConflict('所选事项已不在当前清单，请刷新后重选','task_version_changed')
                published=self._shared_inputs(db,self._materialization.generation(db))['source']
                items=[]
                for identity in identities:
                    card=visible[identity]
                    order_numbers=list(dict.fromkeys(line['order_no'] for line in card['_lines'] if line.get('order_no')))
                    orders=[]
                    for number in order_numbers:
                        source=published.get((card['profile_id'],number),[])
                        orders.append({'order_no':number,'items':[
                            {'record_id':_text(row.get('id')),'sku':_text(row.get('sku')),
                             'product_name':_text(row.get('product_name')),
                             'specification':_text(row.get('specification') or row.get('spec') or row.get('model')),
                             'quantity':_text(row.get('quantity')),
                             'unit':_display_unit(row),
                             'location':_text(row.get('location')),
                             'delivery_date':_day(row.get('delivery_date')),
                             'summary':_text(row.get('summary') or row.get('system_note'))}
                            for row in source]})
                    items.append({'card':self._public(card),'lines':deepcopy(card['_lines']),
                                  'related_groups':deepcopy(card['_related']),'orders':orders})
                return {**deepcopy(meta),'snapshot_id':uuid.uuid4().hex,'exported_at':timestamp(),
                        'role':role,'view':view,'items':items,'total':len(items)}

    def _completion_rows(self, db):
        # Attribute a human completion to its recorded actor, never to the
        # current assignee after a customer transfer. ERP facts stay separate.
        rows = db.execute("""SELECT e.id,e.task_id,e.actor_id,e.event_type,e.note,e.created_at,
            t.role,t.title,t.order_no FROM daily_work_events e
            JOIN daily_work_tasks t ON t.id=e.task_id
            WHERE substr(e.created_at,1,10)=? AND e.event_type IN
            ('resolve','packed','erp_business_completed') ORDER BY e.created_at DESC,e.rowid DESC""",
            (self.daily.today(),))
        unique = {}
        for row in rows:
            business = row['event_type'] == 'erp_business_completed'
            if not business and not row['actor_id']:
                continue
            key = (business,row['role'],'' if business else row['actor_id'],row['task_id'])
            unique.setdefault(key,{**dict(row),'source':'erp' if business else 'human'})
        return list(unique.values())

    def completions(self,user,role,actor='',source='human',page=1):
        if not _is_admin(user):
            raise DailyWorkConflict('管理员权限不足','admin_required')
        if role not in ROLES or source not in {'human','erp'} or int(page)<1:
            raise ValueError('完成记录查询条件无效')
        with self.store.read_snapshot():
            with self.store.connect() as db:
                items=[row for row in self._completion_rows(db) if row['role']==role and
                       row['source']==source and (not actor or row['actor_id']==actor)]
                start=(int(page)-1)*50
                return {'items':items[start:start+50],'total':len(items),'page':int(page),
                        'has_more':start+50<len(items),'business_date':self.daily.today(),
                        'unit':'tasks','source':source}

    def dashboard(self,user):
        if not _is_admin(user):
            raise DailyWorkConflict('管理员权限不足','admin_required')
        self._prepare_views()
        with self.store.read_snapshot():
            with self.store.connect() as db:
                meta=self._meta(db)
                groups=self._board_groups(db,self._shared_inputs(db,self._materialization.generation(db))['groups'])
                staff={row['id']:dict(row) for row in db.execute('SELECT * FROM staff_users WHERE active=1')}
                completed=defaultdict(int)
                business_completed=defaultdict(int)
                today=self.daily.today()
                for row in self._completion_rows(db):
                    if row['source']=='erp':business_completed[row['role']]+=1
                    else:completed[(row['role'],row['actor_id'])]+=1
                actions={}
                for row in db.execute("""SELECT t.role,e.actor_id,max(e.created_at) AS last_at
                    FROM daily_work_events e JOIN daily_work_tasks t ON t.id=e.task_id
                    WHERE e.actor_id<>'' AND e.event_type IN
                    ('start','progress','wait','resolve','packed','reopen') GROUP BY t.role,e.actor_id"""):
                    actions[(row['role'],row['actor_id'])]=row['last_at']
                for row in db.execute("""SELECT actor_id,max(created_at) AS last_at
                    FROM customer_order_followup_events WHERE actor_id<>''
                    AND waiting_for NOT LIKE '负责人转交:%'
                    AND waiting_for NOT LIKE '客户固定负责人变更:%' GROUP BY actor_id"""):
                    key=('sales',row['actor_id'])
                    actions[key]=max(actions.get(key,'') or '',row['last_at'] or '')
                for row in db.execute('SELECT actor_id,max(created_at) AS last_at FROM customer_order_work_actions GROUP BY actor_id'):
                    key=('sales',row['actor_id'])
                    actions[key]=max(actions.get(key,'') or '',row['last_at'] or '')
                roles=[]
                for role in ROLES:
                    cards,_=self._build(db,user,role,'role',phased=role in TWO_STAGE_ROLES)
                    summary={'total':len(cards),'todo':0,'in_progress':0,'waiting':0,
                             'overdue':0,'unassigned':0,
                             'business_completed_today':business_completed[role]}
                    loads=defaultdict(list)
                    for card in cards:
                        status=card['work_state'] if role in TWO_STAGE_ROLES else card['status']
                        if status in summary:summary[status]+=1
                        if card['due_date'] and card['due_date']<today:summary['overdue']+=1
                        owners={line.get('assignee_id') for line in card['_lines'] if line.get('assignee_id')}
                        if not owners and card.get('assignee_id'):owners={card['assignee_id']}
                        if card.get('deputy_id'):owners.add(card['deputy_id'])
                        if not owners:summary['unassigned']+=1
                        for owner in owners:loads[owner].append(card)
                    people=[]
                    for identity,person in sorted(staff.items(),key=lambda value:(value[1]['display_name'],value[0])):
                        if role not in _json(person['roles'],[]):continue
                        assigned=loads[identity]
                        people.append({'user_id':identity,'display_name':person['display_name'],
                            'todo':sum((card['work_state'] if role in TWO_STAGE_ROLES else card['status'])=='todo' for card in assigned),
                            'in_progress':sum((card['work_state'] if role in TWO_STAGE_ROLES else card['status'])=='in_progress' for card in assigned),
                            'waiting':sum(card['status']=='waiting' for card in assigned) if role not in TWO_STAGE_ROLES else 0,
                            'overdue':sum(bool(card['due_date'] and card['due_date']<today) for card in assigned),
                            'total':len(assigned),'completed_today':completed.get((role,identity),0),
                            'completed_today_unit':'tasks',
                            'last_action_at':actions.get((role,identity),'')})
                    roles.append({'role':role,'summary':summary,'people':people,
                                  'board':{'role':role,'view':'role'}})
                company={'customer_orders':len(groups),
                         'unfinished_customer_orders':sum(group['status']!='completed' for group in groups.values()),
                         'ready_to_ship':sum(group['status']=='ready_to_ship' for group in groups.values()),
                         'needs_review':sum(group['status']=='review' for group in groups.values()),
                         'history_coverage':'current_snapshot_only'}
                if self.history_archive:
                    profiles={group['profile_id'] for group in groups.values()}
                    profiles.update(row[0] for row in db.execute(
                        'SELECT DISTINCT profile_id FROM order_history_coverage'))
                    archives=[self.history_archive.coverage(profile,year=int(today[:4]))
                              for profile in sorted(profiles)]
                    archived=sum(item['archived_orders'] for item in archives)
                    company['history_coverage']='partial_archive' if archived else 'current_snapshot_only'
                    company['archived_orders_unmatched']=archived
                    company['history_review_required']=True
                    company['history_archives']=archives
                purchase=self.purchase.status(user) if self.purchase else {'status':'unavailable'}
                return {**meta,'company':company,'roles':roles,
                        'purchase_snapshot':purchase,
                        'purchase_source_updated_at':purchase.get('captured_at_utc',''),
                        'refreshed_at':timestamp()}

    def history(self,user,role,view='mine',scope='processed',page=1):
        self._authorize(user,role,view)
        if scope not in {'processed','archived','all'}:
            raise ValueError('历史范围无效')
        if int(page)<1:
            raise ValueError('页码必须为正整数')
        page=int(page)
        actor=_user_id(user)
        where=['t.role=?'];params=[role]
        if view=='mine':
            where.append('(t.assignee_id=? OR EXISTS (SELECT 1 FROM daily_work_events mine '
                         'WHERE mine.task_id=t.id AND mine.actor_id=?))')
            params.extend([actor,actor])
        if scope=='processed':
            where.append("(t.status<>'todo' OR t.condition_state<>'active' OR EXISTS "
                         "(SELECT 1 FROM daily_work_events e WHERE e.task_id=t.id))")
        elif scope=='archived':
            where.append("t.condition_state<>'active'")
        predicate=' AND '.join(where)
        with self.store.connect() as db:
            total=db.execute('SELECT count(*) FROM daily_work_tasks t WHERE '+predicate,params).fetchone()[0]
            rows=db.execute('SELECT t.* FROM daily_work_tasks t WHERE '+predicate+
                ' ORDER BY t.updated_at DESC,t.rowid DESC LIMIT ? OFFSET ?',
                [*params,30,(page-1)*30]).fetchall()
            staff={row['id']:dict(row) for row in db.execute('SELECT id,display_name FROM staff_users')}
            items=[self.daily._card(row,staff) for row in rows]
            return {**self._meta(db),'items':items,'total':total,'page':page,
                    'page_size':30,'has_more':page*30<total,'scope':scope,'role':role}

    def history_detail(self,user,identity,role,view='mine',page=1):
        self._authorize(user,role,view)
        if int(page)<1:raise ValueError('页码必须为正整数')
        page=int(page)
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM daily_work_tasks WHERE id=? AND role=?',(identity,role)).fetchone()
            if row is None:raise KeyError('历史事项不存在')
            participated=db.execute('SELECT 1 FROM daily_work_events WHERE task_id=? AND actor_id=? LIMIT 1',
                                    (identity,_user_id(user))).fetchone()
            if view=='mine' and row['assignee_id']!=_user_id(user) and not participated:
                raise DailyWorkConflict('没有查看该历史事项的权限','role_forbidden')
            staff={person['id']:dict(person) for person in db.execute('SELECT id,display_name FROM staff_users')}
            events=db.execute('SELECT * FROM daily_work_events WHERE task_id=? '
                'ORDER BY created_at DESC,rowid DESC LIMIT 101 OFFSET ?',
                (identity,(page-1)*100)).fetchall()
            return {**self._meta(db),'item':self.daily._card(row,staff),
                    'events':[dict(event) for event in events[:100]],'page':page,
                    'has_more':len(events)>100}

    def select(self,user,body):
        role,view=_text(body.get('role')),_text(body.get('view')) or 'mine'
        purpose=_text(body.get('purpose') or body.get('action')) or 'action'
        if purpose not in {'action','print','image_export'}:raise ValueError('请选择有效选择用途')
        query=_packing_query(role,body.get('packing_mode',''),**{key:body.get(key,'') for key in PACKING_FILTERS})
        if purpose!='action' and (role not in PACKING_ROLES or query is None):
            raise ValueError('打印及导图选择需要包装打印视图')
        if not body.get('data_version') or body.get('task_version') is None:
            raise ValueError('请选择当前数据版本')
        if query is not None and not body.get('expected_print_state_version'):
            raise ValueError('请选择当前打印状态版本')
        if body.get('all') and view!='mine':raise ValueError('全选请切换为本人任务')
        self._prepare_views()
        with self.store.read_snapshot(),self.store.connect() as db:
            phase=_text(body.get('work_state'))
            if query is None:
                phase=phase or _phase_from_id(body.get('board_id')) or next((_phase_from_id(identity) for identity in body.get('ids',[]) if _phase_from_id(identity)),'')
            if phase and (phase not in {'todo','in_progress','exception'} or role not in TWO_STAGE_ROLES):
                raise ValueError('该岗位不支持此处理阶段')
            snapshot_stamp=tuple(db.execute('SELECT incarnation,generation FROM task_board_materialization_generation WHERE singleton=1').fetchone())
            cards,meta=self._build(db,user,role,view,phased=bool(phase or query is not None),packing_query=query)
            self._assert_version(meta,body.get('data_version'),body.get('task_version'),body.get('expected_print_state_version',''))
            if query is not None and not meta['tasks_current']:
                raise DailyWorkConflict('任务尚未跟上已发布数据，请刷新','task_version_changed')
            if query is None:cards=self._filtered(cards,body.get('keyword',''),body.get('status','all'),body.get('due_from',''),body.get('due_to',''),body.get('assignee_id',''))
            if phase:cards=[card for card in cards if card['work_state']==phase]
            if body.get('board_id'):
                cards=[card for card in cards if card['id']==body['board_id']]
            elif not _flag(body.get('all')):
                ids=set(body.get('ids') or [])
                available={card['id'] for card in cards}
                if not ids or not ids<=available:raise DailyWorkConflict('所选事项已不在当前清单，请刷新', 'task_version_changed')
                cards=[card for card in cards if card['id'] in ids]
        with self.store.connect(write=True) as db:
            stamp=tuple(db.execute('SELECT incarnation,generation FROM task_board_materialization_generation WHERE singleton=1').fetchone())
            if stamp!=snapshot_stamp:
                raise DailyWorkConflict('所选范围或权限已变化，请刷新后重新选择','task_version_changed')
            self._assert_version(self._meta(db),meta['data_version'],meta['task_version'],meta['print_state_version'])
            selected=[];orders=[];profiles=set();excluded=0;card_ids=[]
            chosen_tasks=set(body.get('task_ids') or [])
            if chosen_tasks and not body.get('board_id'):raise ValueError('选择订单明细必须指定商品事项')
            seen_tasks=set()
            for card in cards:
                refs=[]
                if role=='sales':
                    if card['can_act']:refs=card['_refs']
                    elif not body.get('all'):raise DailyWorkConflict('包含其他员工或未分配事项','task_owner_conflict')
                else:
                    eligible_lines=[]
                    for line in card['_lines']:
                        if chosen_tasks and line['task_id'] not in chosen_tasks:continue
                        seen_tasks.add(line['task_id'])
                        eligible=(line.get('can_print') if purpose=='print' else True if purpose=='image_export' else line['can_act'])
                        if not eligible:
                            if purpose=='print':
                                code='print_state_conflict' if line['print_state']=='reserved' else 'task_owner_conflict'
                                raise DailyWorkConflict(line.get('print_blocked_by') or '包含他人、未分配或需核对事项，请重新选择',code)
                            if not body.get('all'):raise DailyWorkConflict('包含他人、已完成或需核对事项，请重新选择','task_owner_conflict')
                            excluded+=1;continue
                        eligible_lines.append(line)
                    wanted={line['task_id'] for line in eligible_lines}
                    refs=[ref for ref in card['_refs'] if ref['id'] in wanted]
                    orders.extend(line['order_no'] for line in eligible_lines if line['order_no'])
                if refs:
                    selected.extend(refs);profiles.add(card['profile_id']);card_ids.append(card['id'])
            if chosen_tasks-seen_tasks:raise DailyWorkConflict('所选订单明细已变化','task_version_changed')
            if not selected:raise ValueError('当前范围没有可操作事项')
            if len(profiles)!=1:raise ValueError('一次批量操作只能选择同一账套')
            payload={'objects':selected,'card_ids':card_ids,'order_nos':sorted(set(orders)),'profile_id':next(iter(profiles)),
                'print_state_version':meta['print_state_version'],'purpose':purpose}
            if query is not None:payload.update(packing_mode='print',query=query,work_state=phase)
            identity=uuid.uuid4().hex
            db.execute('INSERT INTO daily_work_selections VALUES (?,?,?,?,?,?,?)',
                (identity,_user_id(user),role,meta['data_version'],meta['task_version'],dumps(payload),timestamp()))
            return {'selection_id':identity,'object_count':len(selected),'order_nos':payload['order_nos'],
                'order_count':len(payload['order_nos']),'card_count':len(card_ids),'purpose':purpose,
                'data_version':meta['data_version'],'task_version':meta['task_version'],'role':role,
                'print_state_version':meta['print_state_version'],
                'profile_id':payload['profile_id'],'excluded_count':excluded}

    def batch(self,user,body):
        key=_text(body.get('idempotency_key'));action=_text(body.get('action'))
        if not key or len(key)>128:raise ValueError('必须提供有效幂等标识')
        if action not in {'start','packed','followup'}:raise ValueError('批量操作无效')
        fingerprint=_hash(body)
        with self.store.connect(write=True) as db:
            replay=db.execute('SELECT * FROM daily_work_batches WHERE actor_id=? AND idempotency_key=?',(_user_id(user),key)).fetchone()
            if replay:
                if replay['request_hash']!=fingerprint:raise DailyWorkConflict('幂等标识已用于不同操作','idempotency_conflict')
                return {**_json(replay['result'],{}),'replayed':True}
            if _flag(_setting(db,'restoration_hold',False)):
                raise DailyWorkConflict('备份恢复后需核对工作记录，暂不能提交新操作','restoration_hold')
            selection=db.execute('SELECT * FROM daily_work_selections WHERE id=? AND actor_id=?',(_text(body.get('selection_id')),_user_id(user))).fetchone()
            if not selection:raise KeyError('所选范围不存在，请重新选择')
            role=selection['role'];self._authorize(user,role,'mine')
            meta=self._meta(db);self._assert_version(meta,selection['data_version'])
            if not meta['tasks_current']:raise DailyWorkConflict('任务尚未跟上已发布数据，请刷新','task_version_changed')
            if action in {'start','packed'} and role not in PACKING_ROLES:raise ValueError('该操作仅用于包装岗位')
            if action=='followup' and role in PACKING_ROLES:raise ValueError('包装请使用开始或已包装操作')
            payload=_json(selection['payload'],{})
            if payload.get('purpose','action')!='action':
                raise DailyWorkConflict('该选择范围仅用于打印或只读导图，不能提交工作状态操作','selection_purpose_conflict')
            result_label=_text(body.get('result'));note=_text(body.get('note'))
            waiting=_text(body.get('waiting_for'));promised=_text(body.get('promised_date'));follow=_text(body.get('next_follow_up_date'))
            if action=='followup':
                if not result_label:raise ValueError('请选择本次处理结果')
                if result_label in {'其他原因','需核对'} and not note:raise ValueError('请简要说明原因')
                if promised and not _day(promised) or follow and not _day(follow):raise ValueError('日期必须为 YYYY-MM-DD')
                if len(waiting)>200 or len(note)>1000 or len(result_label)>100:raise ValueError('处理说明过长')
                follow=follow or _next_workday(self.daily.today())
            # Entire selection is checked and applied in this single transaction.
            # Any conflict rolls back every event and state, including replay logs.
            for index,ref in enumerate(payload['objects']):
                if ref['kind']=='sales':
                    response=self.customers.followup(user,ref['id'],_hash([_user_id(user),key,ref['id']]),ref['version'],
                        result_label+('：'+note if note else ''),waiting,follow,db=db)
                    db.execute("UPDATE customer_order_followups SET status='waiting' WHERE group_id=?",(ref['id'],))
                    ref['version']=response['version']
                else:
                    response=self.daily.action(ref['id'],user,'wait' if action=='followup' else action,
                        _hash([_user_id(user),key,ref['id']]),ref['version'],result_label+('：'+note if note else '') if action=='followup' else note,
                        follow,db=db)
                    ref['version']=response['task']['version']
                    if action=='followup':
                        task_payload=response['task']['payload']
                        task_payload.update(waiting_for=waiting,promised_date=promised,followup_result=result_label,followup_source_changed=False)
                        db.execute('UPDATE daily_work_tasks SET payload=? WHERE id=?',(dumps(task_payload),ref['id']))
            if action=='followup':
                for identity in payload['card_ids']:
                    db.execute('INSERT INTO task_board_followups VALUES (?,?,?,?,?,?) ON CONFLICT(object_id) DO UPDATE SET result=excluded.result,waiting_for=excluded.waiting_for,promised_date=excluded.promised_date,note=excluded.note,updated_at=excluded.updated_at',
                        (identity,result_label,waiting,promised,note,timestamp()))
            current=self._meta(db)
            db.execute('UPDATE daily_work_selections SET payload=?,task_version=? WHERE id=?',(dumps(payload),current['task_version'],selection['id']))
            response={'processed':len(payload['objects']),'task_version':current['task_version'],'replayed':False}
            db.execute('INSERT INTO daily_work_batches VALUES (?,?,?,?,?)',(key,_user_id(user),fingerprint,dumps(response),timestamp()))
            return response


def task_board_routes(engine):
    bp=Blueprint('task_board',__name__)
    def respond(call):
        try:return jsonify(ok=True,**call())
        except DailyWorkConflict as error:return jsonify(ok=False,error=error.code,message=str(error)),(403 if error.code in {'role_forbidden','task_owner_conflict','admin_required'} else 409)
        except KeyError as error:return jsonify(ok=False,error='not_found',message=str(error).strip("'")),404
        except (ValueError,TypeError) as error:return jsonify(ok=False,error='invalid_request',message=str(error)),400
    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):raise ValueError('请求必须为 JSON 对象')
        return value
    @bp.get('/api/v2/daily-work/board')
    def board():
        return respond(lambda:engine.list(g.current_user,request.args.get('role',''),
            request.args.get('view','mine'),request.args.get('keyword',''),request.args.get('status','all'),
            request.args.get('page',1),request.args.get('data_version',''),request.args.get('task_version'),
            request.args.get('due_from',''),request.args.get('due_to',''),request.args.get('assignee_id',''),request.args.get('work_state',''),request.args.get('print_state_version',''),
            packing_mode=request.args.get('packing_mode',''),customer=request.args.get('customer',''),sku=request.args.get('sku',''),order_no=request.args.get('order_no',''),
            order_date_from=request.args.get('order_date_from',''),order_date_to=request.args.get('order_date_to',''),print_state=request.args.get('print_state','all'),
            sort=request.args.get('sort','default'),page_size=request.args.get('page_size'),
            customer_key=request.args.get('customer_key',''),order_no_match=request.args.get('order_no_match',''),
            supplier=request.args.get('supplier',''),receipt_from=request.args.get('receipt_from',''),receipt_to=request.args.get('receipt_to',''),
            compact=request.args.get('compact')=='1'))
    @bp.get('/api/v2/daily-work/board/filter-options')
    def filter_options():return respond(lambda:engine.filter_options(g.current_user,request.args.get('role',''),request.args.get('view','mine')))
    @bp.get('/api/v2/daily-work/board/<identity>')
    def detail(identity):return respond(lambda:engine.detail(g.current_user,identity,request.args.get('role',''),request.args.get('view','mine'),request.args.get('work_state',''),request.args.get('print_state_version',''),
        packing_mode=request.args.get('packing_mode',''),**{key:request.args.get(key,'') for key in PACKING_FILTERS}))
    @bp.get('/api/v2/daily-work/dashboard')
    def dashboard():return respond(lambda:engine.dashboard(g.current_user))
    @bp.get('/api/v2/daily-work/dashboard/completions')
    def completions():return respond(lambda:engine.completions(g.current_user,
        request.args.get('role',''),request.args.get('actor_id',''),
        request.args.get('source','human'),request.args.get('page',1)))
    @bp.get('/api/v2/daily-work/task-history')
    def task_history():return respond(lambda:engine.history(g.current_user,
        request.args.get('role',''),request.args.get('view','mine'),
        request.args.get('scope','processed'),request.args.get('page',1)))
    @bp.get('/api/v2/daily-work/task-history/<identity>')
    def task_history_detail(identity):return respond(lambda:engine.history_detail(g.current_user,identity,
        request.args.get('role',''),request.args.get('view','mine'),request.args.get('page',1)))
    @bp.post('/api/v2/daily-work/export-snapshot')
    def export_snapshot():return respond(lambda:engine.export_snapshot(g.current_user,body()))
    @bp.post('/api/v2/daily-work/selections')
    def selection():return respond(lambda:engine.select(g.current_user,body()))
    @bp.post('/api/v2/daily-work/batches')
    def batch():return respond(lambda:engine.batch(g.current_user,body()))
    @bp.post('/api/v2/daily-work/groups/<identity>/actions')
    def sales_action(identity):return respond(lambda:engine.sales_action(g.current_user,identity,body()))
    return bp
