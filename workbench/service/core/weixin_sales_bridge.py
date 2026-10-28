"""Owner Weixin entry point over the SAME versioned SalesAssistant as Android.

No model output is a command. Only exact owner confirmation messages consume a
server-issued, displayed, version-bound approval. Submission uses the existing
browser queue; it never calls ERP or retries an unknown website upload itself.
"""
from io import BytesIO
import json
import re
import secrets
import time
from werkzeug.datastructures import FileStorage

from .sales_assistant import SalesAssistantError
from tools.weixin_sales_spool import digest


HELP=('微信录单已接入 App 同一份数据。\n'
      '1. 发「录单 客户名称」，选定客户。\n'
      '2. 发订单文字、图片或 Excel（单份≤10MiB），可连续补充。\n'
      '3. 整理后核对完整草稿，回复「确认上传 确认码」才会上传。\n'
      '「草稿」查看当前稿；「录单状态」查看进度；「我的录单」切换已有会话。\n'
      '「准备新品 建议编号」只生成方案；「确认新建 确认码」才创建编码。\n'
      '「结束录单」仅退出当前会话，资料仍保留。采购尚未接入。')
LOCKED={'submitting','submitted','submission_unknown','submission_blocked'}


class BridgeError(ValueError): pass


def clean(value,limit=300):
    return ''.join(c for c in str(value or '') if c in '\n\t' or ord(c)>=32)[:limit]


class SalesBridge:
    def __init__(self,engine,spool,*,actor_id,profile_id,binding,download,invoke=None,clock=time.time):
        self.engine,self.spool=engine,spool
        self.actor={'id':actor_id,'active':True};self.profile=profile_id;self.binding=binding
        self.download=download;self.invoke=invoke or (lambda fn,*args,**kw:fn(*args,**kw));self.clock=clock
        self._poll_signatures={}

    def read(self,query,params=()):
        def select():
            with self.engine.store.connect() as db:return [tuple(r) for r in db.execute(query,params)]
        return self.invoke(select)

    def fingerprint(self,identity):
        # Cheap indexed lookups; unchanged sessions never rebuild matching projections.
        values=[]
        for query,params in (
            ('SELECT version,status,updated_at FROM sales_assistant_sessions WHERE id=?',(identity,)),
            ('SELECT id,status,updated_at FROM sales_codex_jobs WHERE session_id=?',(identity,)),
            ('SELECT id,status,updated_at FROM sales_new_product_plans WHERE session_id=?',(identity,)),
            ('SELECT status,result_json FROM web_operations WHERE id=(SELECT submission_operation_id FROM sales_assistant_sessions WHERE id=?)',(identity,)),
            ("SELECT value FROM settings WHERE key IN ('data_version','sales_codex_policy','sales_web_import_policy')",()),
            ('SELECT o.owner_id,o.status,o.updated_at FROM sales_customer_owners o JOIN sales_assistant_sessions s ON s.profile_id=o.profile_id AND s.customer_name=o.customer_name WHERE s.id=?',(identity,))):
            values.append(self.read(query,params))
        return digest(values)

    def call(self,method,*args):
        return self.invoke(getattr(self.engine,method),self.actor,*args)

    def session(self,identity=None):
        identity=identity or self.spool.get('active_session')
        if not identity:raise BridgeError('请先发「录单 客户名称」，也可先发资料再指定客户。')
        value=self.call('detail',identity)['session']
        if value['profile_id']!=self.profile:raise BridgeError('会话不属于当前账套。')
        return value

    def reply(self,key,text,dependency=''):
        self.spool.reply(key,self.binding,text,dependency)

    def action(self,key,method,session_id,body,*,extra='',file_factory=None):
        """Persist exact request BEFORE calling the existing idempotent business API."""
        with self.spool.connect() as db:
            row=db.execute('SELECT * FROM actions WHERE id=?',(key,)).fetchone()
            if row:
                if row['method']!=method or row['session_id']!=session_id:raise BridgeError('操作标识冲突，请查看原操作。')
                packet=json.loads(row['request'])
                if row['result']:return json.loads(row['result'])
            else:
                packet={'body':{**body,'idempotency_key':'wx-'+digest(key)[:48]},'extra':extra}
                db.execute('INSERT INTO actions(id,method,session_id,request,created) VALUES (?,?,?,?,?)',
                    (key,method,session_id,json.dumps(packet,ensure_ascii=False),self.clock()))
        args=[session_id] if session_id else []
        if packet['extra']:args.append(packet['extra'])
        args.append(packet['body'])
        if file_factory:args.append(file_factory())
        result=self.call(method,*args)
        compact={k:result[k] for k in ('job_id','plan_id','operation_id','erp_order_no','duplicate_of') if k in result}
        compact['session']={k:result['session'][k] for k in ('id','version','status','data_version')}
        with self.spool.connect() as db:
            db.execute('UPDATE actions SET result=? WHERE id=?',(json.dumps(compact,ensure_ascii=False),key))
        return compact

    def body(self,s):
        return {'version':s['version'],'data_version':s['data_version']}

    def choose_customer(self,query,key):
        found=self.call('customers',self.profile,query,1)
        exact=[x for x in found['items'] if x['customer_name']==query]
        if exact:
            return self.start(exact[0]['customer_name'],key)
        choices=found['items'][:10]
        if not choices:raise BridgeError('没有找到已有固定销售的客户。请发完整名称；归属未确认的客户需先在 App 设置中指定。')
        self.spool.set('customer_choices',{'items':choices,'expires':self.clock()+600})
        self.reply(key+'-choices','请确认客户：\n'+'\n'.join(
            f"{i}. {x['customer_name']}（{x['owner_name']}）" for i,x in enumerate(choices,1))+
            '\n回复「选择客户 1」等序号；仅本次十分钟内有效。'+
            ('\n匹配超过10家，请补充更完整的客户名称。' if found['total']>10 else ''))

    def start(self,customer,key):
        result=self.action(key+'-create','create','',{'profile_id':self.profile,'customer_name':customer})
        identity=result['session']['id'];self.spool.set('active_session',identity)
        self.spool.set('customer_choices',None)
        self.spool.release_held(self.binding,identity)
        self.watch(identity)
        self.reply(key+'-started',f'已开始录单：{customer}\n会话 {identity[:8]}，App「录单」中可查看同一份资料。\n请发送文字、图片或 Excel，可连续补充。')
        return identity

    def watch(self,identity):
        values=self.spool.get('watched_sessions',[])
        self.spool.set('watched_sessions',([identity]+[v for v in values if v!=identity])[:30])

    def dirty(self,identity):
        self.spool.set('dirty:'+identity,{'after':self.clock()+6,'nonce':secrets.token_hex(8)})
        self.watch(identity)

    def input(self,event):
        key=event['id'];payload=json.loads(event['payload'])
        if payload.get('error'):raise BridgeError(payload['error'])
        items=payload.get('items',[])
        text='\n'.join(item.get('text_item',{}).get('text','') for item in items
                       if isinstance(item,dict) and item.get('type')==1)
        if len(text)>10000:raise BridgeError('单次文字超过10000字，请拆开发送。')
        media=[item for item in items if isinstance(item,dict) and item.get('type') in (2,4)]
        unsupported=[item for item in items if not isinstance(item,dict) or item.get('type') not in (1,2,4)]
        # Executable commands must be the entire plain-text message, not a quote or attachment.
        command=text.strip() if not media and not unsupported and '\n' not in text.strip() else ''
        if command in ('录单帮助','帮助','开始录单','录单'):
            self.reply(key+'-help',HELP);return
        match=re.fullmatch(r'录单\s+(.{1,200})',command)
        if match:self.choose_customer(match[1].strip(),key);return
        match=re.fullmatch(r'选择客户\s+(\d{1,2})',command)
        if match:
            choices=self.spool.get('customer_choices') or {}
            index=int(match[1])-1
            if choices.get('expires',0)<self.clock() or not 0<=index<len(choices.get('items',[])):
                raise BridgeError('客户选项已失效，请重新发「录单 客户名称」。')
            self.start(choices['items'][index]['customer_name'],key);return
        if command=='我的录单':
            rows=[s for s in self.call('list',1)['items'] if s['profile_id']==self.profile][:15]
            self.reply(key+'-list','最近录单：\n'+'\n'.join(f"{s['id'][:8]} {s['customer_name']} · {s['status']}" for s in rows)+
                       '\n回复「打开 会话编号」，或在 App 继续编辑。');return
        match=re.fullmatch(r'打开\s+([0-9a-fA-F]{8,32})',command)
        if match:
            # Restrict SQL to IDs, then use the SAME role/customer checks as App.
            ids=[r[0] for r in self.read('SELECT id FROM sales_assistant_sessions WHERE profile_id=? AND id LIKE ?',
                                       (self.profile,match[1].lower()+'%'))]
            if len(ids)!=1:raise BridgeError('会话编号不唯一或不存在，请发「我的录单」查看。')
            s=self.session(ids[0]);self.spool.set('active_session',s['id']);self.watch(s['id'])
            self.show(s,key);return
        if command=='结束录单':
            self.spool.set('active_session',None);self.reply(key+'-end','已退出当前录单。草稿和原资料仍保留在 App；再次录单请指定客户。');return
        if command in ('草稿','录单状态'):
            self.show(self.session(),key);return
        match=re.fullmatch(r'确认(上传|新建)\s+([0-9A-Fa-f]{8})',command)
        if match:self.confirm(match[2].upper(),'upload' if match[1]=='上传' else 'product',key);return
        if command.startswith(('确认上传','确认新建')):
            raise BridgeError('请使用当前草稿末尾的完整确认码，例如「确认上传 ABCD1234」。普通“确认”不会上传。')
        match=re.fullmatch(r'准备新品\s+([0-9a-fA-F]{8,32})(?:\s+([A-Za-z0-9_-]{1,100}))?',command)
        if match:
            s=self.session();job=s.get('codex_job') or {}
            values=[v for v in job.get('new_product_suggestions',[]) if v['id'].startswith(match[1].lower())]
            if len(values)!=1:raise BridgeError('新商品建议已变化，请查看当前草稿。')
            result=self.action(key+'-prepare','prepare_new_product',s['id'],{**self.body(s),
                'job_id':job['id'],'result_hash':job['result_hash'],'suggestion_id':values[0]['id'],
                'reference_code':match[2] or ''})
            self.watch(s['id']);self.reply(key+'-prepare-reply','正在查重并准备新编码方案，尚未创建；完成后会另发确认码。');return
        if unsupported:raise BridgeError('当前微信录单支持文字、图片和 Excel；语音、视频及合并转发请先转成这些资料。')
        if not text.strip() and not media:raise BridgeError('消息中没有可读取的订单资料。')
        identity=event['session_id'] or self.spool.get('active_session')
        if not identity:
            self.spool.input_state(key,'held')
            self.reply(key+'-held','资料已暂存。请发「录单 客户名称」，选定后会把这批资料放入同一份草稿。');return
        self.spool.input_state(key,'processing',identity)
        s=self.session(identity)
        if s['status'] in LOCKED or s.get('submission_operation_id'):
            raise BridgeError('该会话已提交或结果待核对，不能追加。请发「录单 客户名称」建立新单；本次消息已保留。')
        if text.strip():
            self.action(key+'-text','message',identity,{**self.body(s),'text':text.strip()})
        for index,item in enumerate(media):
            s=self.session(identity)
            def file_factory(item=item):
                name,raw=self.download(item)
                return FileStorage(stream=BytesIO(raw),filename=name)
            self.action(key+f'-file-{index}','attachment',identity,self.body(s),file_factory=file_factory)
        self.dirty(identity)
        self.reply(key+'-received',f"已保存到 {s['customer_name']} · {identity[:8]}。正在按 NAS 规则整理，完成后会发草稿；还可以继续补充。")

    def process_one(self):
        event=self.spool.next_input(self.binding)
        if not event:return False
        try:
            self.input(event)
            with self.spool.connect() as db:
                status=db.execute('SELECT status FROM inbox WHERE id=?',(event['id'],)).fetchone()[0]
            if status!='held':self.spool.input_state(event['id'],'done')
        except (SalesAssistantError,BridgeError,ValueError,KeyError) as error:
            message=clean(str(error),900)
            if isinstance(error,KeyError):message='会话或资料已变化，请在 App 核对；原输入没有自动重交。'
            self.reply(event['id']+'-error','录单待核对：'+message+'\n可发「草稿」查看；原资料保留，未绕过校验执行。')
            self.spool.input_state(event['id'],'error')
        return True

    def status_text(self,s):
        state=s.get('submission_state')
        if s.get('submission_operation_id'):
            labels={'queued':'等待网页上传','running':'正在网页上传','succeeded':'网页已确认导入',
                    'uncertain':'导入结果待核对，请勿重复上传','blocked':'上传被阻止，需核对原因','failed':'上传未完成，请核对原操作'}
            text=labels.get(state,'提交结果待核对')
            if state=='succeeded' and s.get('submission_completion_basis')!='web_import_confirmation':
                text='导入结果待核对'
            return f"{s['customer_name']} · {s['id'][:8]}\n{text}\n"+(
                '单号：'+'、'.join(s.get('erp_order_numbers',[])[:12]) if state=='succeeded' else '')
        job=s.get('codex_job') or {}
        if job.get('status') in ('queued','running'):
            return f"{s['customer_name']} · {s['id'][:8]}\n"+('等待 Codex 整理' if job['status']=='queued' else '正在分析资料、核对商品编码')
        return ''

    def show(self,s,key):
        status=self.status_text(s)
        if status:self.reply(key+'-status',status);return
        job=s.get('codex_job') or {}
        if job.get('status') in ('needs_clarification','failed','stale') and job.get('base_version')==s['version']:
            lines=[f"{s['customer_name']} · {s['id'][:8]}，还需核对：",clean(job.get('assistant_text'),900)]
            for value in job.get('questions',[])[:12]:
                lines.append(clean(value if isinstance(value,str) else value.get('question') or value.get('message') or json.dumps(value,ensure_ascii=False),220))
            for value in job.get('new_product_suggestions',[])[:12]:
                lines.append(f"新品建议 {value['id'][:8]}：{clean(value.get('product_name'))} {clean(value.get('spec'))}。可发「准备新品 {value['id'][:8]}」。")
            lines.append('直接补充文字或原文件即可继续；也可在 App 查看完整资料。')
            self.reply(key+'-questions','\n'.join(lines)[:3450]);return
        proposed=bool(job.get('can_adopt'))
        draft=job.get('preview',{}) if proposed else s['draft']
        rows=draft.get('lines',[])
        issues=draft.get('issues',[])
        if not rows or issues:
            detail='\n'.join(clean(v.get('message',v.get('code','待核对')),160) for v in issues[:12])
            self.reply(key+'-issues',f"{s['customer_name']} · {s['id'][:8]}\n"+(detail or '请发送订单资料。')+'\n补充后会重新整理，尚未上传。');return
        frozen={**self.body(s),'owner_id':s['fixed_sales_owner_id'],'draft_hash':digest(s['draft']),
            'job_id':job.get('id') if proposed else '', 'result_hash':job.get('result_hash') if proposed else '',
            'reviewed_hash':digest(draft)}
        header=(f"待确认录单：{s['customer_name']}\n会话 {s['id'][:8]} · v{s['version']} · {len(rows)}行\n"
                f"固定销售/经手人：{s['fixed_sales_owner_name']}\n客户订单号：{clean(draft.get('customer_order_no'))}\n"
                f"交期：{clean(draft.get('delivery_date'))}\n报价：{'未税' if draft.get('tax_basis')=='tax_exclusive' else '含税'}，税率13%\n")
        details=[]
        for i,line in enumerate(rows,1):
            catalog=line.get('catalog_summary') or {}
            details.append(f"{i}. 原文：{clean(line.get('original_name') or line.get('product_name'),120)} "
                f"{clean(line.get('original_spec') or line.get('spec'),120)}\n"
                f"ERP匹配：{clean(line.get('product_code'),80)} {clean(line.get('product_name') or catalog.get('product_name'),120)} "
                f"{clean(line.get('spec') or catalog.get('spec'),120)} {clean(line.get('model') or catalog.get('model'),100)}\n"
                f"标准/等级/材质/表面：{clean(line.get('standard'),80)} / {clean(line.get('grade'),60)} / "
                f"{clean(line.get('original_material'),60)} / {clean(line.get('surface'),80)}\n"
                f"数量 {clean(line.get('quantity'),40)} {clean(line.get('unit') or '支',30)}，报价 {clean(line.get('unit_price'),50)} 元；"
                f"交期 {clean(line.get('delivery_date') or draft.get('delivery_date'),50)}\n"
                f"客户料号/行号：{clean(line.get('customer_part_no'),100)} / {clean(line.get('line_no'),40)}；"
                f"项目/图号：{clean(line.get('project_no'),100)} / {clean(line.get('drawing_no'),100)}\n"
                f"来源：{clean(line.get('source_quote') or line.get('source_preview') or (line.get('source_ref') or {}).get('source_quote'),220)}")
        pages=[];page=header
        for line in details:
            if len(page)+len(line)>2600:pages.append(page);page=header
            page+='\n'+line+'\n'
        if page:pages.append(page)
        pages.append('附加说明：'+clean(draft.get('note'),1600)+'\n请核对每行编码、数量、单价、交期及原文对应关系。')
        self.approval('upload',s,frozen,key,pages)

    def approval(self,kind,s,payload,key,pages):
        existing=self.spool.get('approval-key:'+key)
        code=existing or secrets.token_hex(4).upper();last=f'approval-{code}-final'
        if existing:
            old=self.spool.approval(code)
            if not old or old['binding']!=self.binding or old['kind']!=kind or old['session_id']!=s['id']:
                raise BridgeError('核对稿记录不一致，请重新查看草稿。')
            pages=old['payload']['pages']
        else:
            payload={**payload,'final_reply':last,'pages':pages}
            with self.spool.connect() as db:
                db.execute('INSERT INTO approvals(code,binding,kind,session_id,payload,created,expires) VALUES (?,?,?,?,?,?,?)',
                    (code,self.binding,kind,s['id'],json.dumps(payload,ensure_ascii=False),self.clock(),self.clock()+7200))
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('approval-key:'+key,json.dumps(code)))
        previous=''
        for i,page in enumerate(pages,1):
            identity=f'approval-{code}-{i}'
            self.reply(identity,f'【核对 {i}/{len(pages)}】\n'+page,previous);previous=identity
        verb='上传' if kind=='upload' else '新建'
        ending=(f"仅确认本稿：回复「确认{verb} {code}」。确认码2小时内有效；修改草稿后失效。\n"+
                ('确认后将采用以上编码匹配、校验原格式表格并自动网页上传为未审核订单；不会审核。网页结果不确定时暂停，不自动重传。'
                 if kind=='upload' else '这一步会创建上述新商品编码；创建并核实后，再重新整理订单。'))
        self.reply(last,ending,previous)
        return code

    def confirm(self,code,kind,key):
        approval=self.spool.approval(code)
        if not approval or approval['binding']!=self.binding or approval['kind']!=kind:
            raise BridgeError('确认码无效，未执行。请查看当前草稿。')
        if approval['status']=='done':
            self.reply(key+'-already','这份确认已经处理，未重复执行。\n'+approval['receipt']);return
        if approval['status'] not in ('pending','executing') or approval['expires']<self.clock():
            raise BridgeError('确认码已失效或原操作需核对，请重新查看草稿；不会自动重交。')
        p=approval['payload'];identity=approval['session_id']
        if approval['status']=='pending':
            if not self.spool.delivered(p['final_reply']):raise BridgeError('完整核对稿尚未确认发送成功，请发「草稿」重新查看。')
            s=self.session(identity)
            if (s['version']!=p['version'] or s['data_version']!=p['data_version'] or
                s['fixed_sales_owner_id']!=p['owner_id'] or digest(s['draft'])!=p['draft_hash']):
                raise BridgeError('App 草稿、商品数据或客户归属已变化。请发「草稿」核对新版；旧确认不会执行。')
            if kind=='upload' and p.get('job_id'):
                job=s.get('codex_job') or {}
                if job.get('id')!=p['job_id'] or job.get('result_hash')!=p['result_hash'] or not job.get('can_adopt'):
                    raise BridgeError('编码匹配建议已变化，请重新查看草稿。')
            with self.spool.connect() as db:
                db.execute("UPDATE approvals SET status='executing' WHERE code=? AND status='pending'",(code,))
        try:
            version=p['version']
            if kind=='product':
                self.action('approval:'+code+':product','confirm_new_product',identity,
                    {'version':version,'data_version':p['data_version'],'plan_hash':p['plan_hash']},extra=p['plan_id'])
                receipt='新商品已进入受控创建队列；完成后会核实编码并重新匹配订单。'
            else:
                if p.get('job_id'):
                    result=self.action('approval:'+code+':adopt','adopt_codex',identity,
                        {'version':version,'data_version':p['data_version'],'result_hash':p['result_hash']},extra=p['job_id'])
                    version=result['session']['version']
                result=self.action('approval:'+code+':confirm','confirm',identity,{'version':version})
                version=result['session']['version']
                result=self.action('approval:'+code+':submit','submit',identity,{'version':version})
                receipt='已确认并排入自动网页上传队列。\n会话 '+identity[:8]+'；进度稍后回传，也可在 App 查看。'
            with self.spool.connect() as db:
                db.execute("UPDATE approvals SET status='done',receipt=? WHERE code=?",(receipt,code))
            self.reply(key+'-confirmed',receipt);self.watch(identity)
        except (SalesAssistantError,ValueError,KeyError):
            with self.spool.connect() as db:db.execute("UPDATE approvals SET status='blocked' WHERE code=?",(code,))
            raise

    def tick(self):
        for identity in self.spool.get('watched_sessions',[])[:30]:
            try:
                dirty=self.spool.get('dirty:'+identity)
                fingerprint=self.fingerprint(identity)
                if not dirty and self._poll_signatures.get(identity)==fingerprint:continue
                s=self.session(identity);job=s.get('codex_job') or {}
                if (dirty and dirty['after']<=self.clock() and not s.get('submission_operation_id')
                        and job.get('status') not in ('queued','running')):
                    self.action('analyze:'+identity+':'+dirty['nonce'],'request_codex',identity,self.body(s))
                    self.spool.set('dirty:'+identity,None);s=self.session(identity);job=s.get('codex_job') or {}
                signature=digest([s['version'],s.get('submission_state'),job.get('id'),job.get('status'),
                    [(p['id'],p['status'],p.get('plan_hash')) for p in s.get('new_product_plans',[])]])
                if signature==self.spool.get('seen:'+identity):
                    self._poll_signatures[identity]=fingerprint;continue
                for plan in s.get('new_product_plans',[]):
                    tag='plan:'+plan['id']+':'+plan['status']+':'+plan.get('plan_hash','')
                    if self.spool.get(tag):continue
                    if plan.get('can_confirm'):
                        p={**self.body(s),'owner_id':s['fixed_sales_owner_id'],'draft_hash':digest(s['draft']),
                           'plan_id':plan['id'],'plan_hash':plan['plan_hash']}
                        self.approval('product',s,p,tag,[f"待新建商品：{plan.get('code')}\n名称：{plan.get('product_name')}\n"
                            f"规格：{plan.get('spec')}\n属性：{plan.get('model')}\n单位：{plan.get('base_unit')}\n"
                            f"分类：{plan.get('classification_path')}"])
                    elif plan['status']=='created':
                        self.reply(tag,self.binding_text(s)+f"新编码 {plan.get('created_code')} 已创建并核实，正在重新匹配。")
                        self.dirty(identity)
                    elif plan['status'] in ('unknown','blocked','failed'):
                        self.reply(tag,self.binding_text(s)+'新商品处理需核对：'+clean(plan.get('error_code') or plan.get('issues'),700)+'\n可在 App 查看方案；不会自动重复创建。')
                    self.spool.set(tag,True)
                if s.get('submission_operation_id') or job.get('status') in ('ready_for_review','needs_clarification','failed','stale'):
                    self.show(s,'watch:'+identity+':'+signature)
                self.spool.set('seen:'+identity,signature)
                self._poll_signatures[identity]=fingerprint
            except (SalesAssistantError,ValueError,KeyError) as error:
                signature=digest([type(error).__name__,str(error)])
                if self.spool.get('tick-error:'+identity)!=signature:
                    self.reply('tick-error:'+identity+':'+signature,'录单 '+identity[:8]+' 待核对：'+clean(str(error),700))
                    self.spool.set('tick-error:'+identity,signature)

    @staticmethod
    def binding_text(s):return s['customer_name']+' · '+s['id'][:8]+'\n'
