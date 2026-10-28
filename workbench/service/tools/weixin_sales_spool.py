"""Private, bounded Weixin transport journal. Contains no business credentials.

The notifier authenticates the owner before ingestion. One business worker uses
durable action intents to cross into the existing App APIs without blind retries.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


ROOT = Path('/var/lib/ul-weixin-sales')


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def account_binding(account):
    return digest([account['ilink_user_id'],account['ilink_bot_id']])


class Spool:
    def __init__(self, root=ROOT):
        self.root=Path(root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError('unsafe_weixin_spool')
        self.path=self.root/'bridge.sqlite3'
        if self.path.is_symlink(): raise ValueError('unsafe_weixin_spool')
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS inbox(
                id TEXT PRIMARY KEY,binding TEXT NOT NULL,payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',session_id TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,updated REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS inbox_work ON inbox(status,created);
              CREATE TABLE IF NOT EXISTS actions(
                id TEXT PRIMARY KEY,method TEXT NOT NULL,session_id TEXT NOT NULL,
                request TEXT NOT NULL,result TEXT NOT NULL DEFAULT '',created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS outbox(
                id TEXT PRIMARY KEY,binding TEXT NOT NULL,text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',dependency TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,updated REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS outbox_work ON outbox(status,created);
              CREATE TABLE IF NOT EXISTS business_requests(
                id TEXT PRIMARY KEY,binding TEXT NOT NULL,scope_hash TEXT NOT NULL,message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',receipt TEXT NOT NULL DEFAULT '',expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS approvals(
                code TEXT PRIMARY KEY,binding TEXT NOT NULL,kind TEXT NOT NULL,
                session_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                receipt TEXT NOT NULL DEFAULT '',created REAL NOT NULL,expires REAL NOT NULL);
            ''')
        os.chmod(self.path,0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally: db.close()

    def get(self,key,default=None):
        with self.connect() as db:
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def ingest(self,binding,message):
        identifier=str(message.get('message_id') or message.get('client_id') or '')
        if not identifier or len(identifier)>200: raise ValueError('invalid_message_id')
        identity=digest([binding,identifier])
        items=message.get('item_list',[])
        safe={'items':items if isinstance(items,list) else [],'message_id':identifier}
        if len(json.dumps(safe).encode())>128*1024 or len(safe['items'])>20:
            safe={'items':[],'error':'单条消息资料过多，请分开发送。'}
        with self.connect() as db:
            if db.execute('SELECT 1 FROM inbox WHERE id=?',(identity,)).fetchone(): return identity
            if self.path.stat().st_size>128*1024*1024:
                raise ValueError('weixin_journal_capacity_review')
            if db.execute("SELECT count(*) FROM inbox WHERE status IN ('pending','held','processing')").fetchone()[0]>=200:
                raise ValueError('weixin_inbox_full')
            stamp=time.time()
            db.execute('INSERT INTO inbox(id,binding,payload,created,updated) VALUES (?,?,?,?,?)',
                       (identity,binding,json.dumps(safe,ensure_ascii=False),stamp,stamp))
        return identity

    def next_input(self,binding):
        with self.connect() as db:
            row=db.execute("SELECT * FROM inbox WHERE binding=? AND status IN ('pending','processing') ORDER BY created,rowid LIMIT 1",
                           (binding,)).fetchone()
        return dict(row) if row else None

    def input_state(self,identity,status,session_id=None):
        with self.connect() as db:
            if session_id is None:
                db.execute('UPDATE inbox SET status=?,updated=? WHERE id=?',(status,time.time(),identity))
            else:
                db.execute('UPDATE inbox SET status=?,session_id=?,updated=? WHERE id=?',
                           (status,session_id,time.time(),identity))

    def release_held(self,binding,session_id):
        with self.connect() as db:
            db.execute("UPDATE inbox SET status='pending',session_id=?,updated=? WHERE binding=? AND status='held'",
                       (session_id,time.time(),binding))

    def reply(self,identity,binding,text,dependency=''):
        if not isinstance(text,str) or not text or len(text)>3500: raise ValueError('invalid_reply_size')
        with self.connect() as db:
            row=db.execute('SELECT text,binding,dependency FROM outbox WHERE id=?',(identity,)).fetchone()
            if row:
                if tuple(row)!=(text,binding,dependency): raise ValueError('reply_identity_conflict')
                return
            stamp=time.time()
            db.execute('INSERT INTO outbox(id,binding,text,dependency,created,updated) VALUES (?,?,?,?,?,?)',
                       (identity,binding,text,dependency,stamp,stamp))

    def next_reply(self,binding):
        with self.connect() as db:
            row=db.execute('''SELECT o.* FROM outbox o WHERE o.binding=? AND o.status IN ('pending','sending')
                AND (o.dependency='' OR EXISTS(SELECT 1 FROM outbox p WHERE p.id=o.dependency AND p.status='accepted'))
                ORDER BY o.created,o.rowid LIMIT 1''',(binding,)).fetchone()
        return dict(row) if row else None

    def reply_state(self,identity,status):
        with self.connect() as db:
            db.execute('UPDATE outbox SET status=?,updated=? WHERE id=?',(status,time.time(),identity))

    def approval(self,code):
        with self.connect() as db:
            row=db.execute('SELECT * FROM approvals WHERE code=?',(code,)).fetchone()
        return {**dict(row),'payload':json.loads(row['payload'])} if row else None

    def delivered(self,identity):
        with self.connect() as db:
            row=db.execute('SELECT status FROM outbox WHERE id=?',(identity,)).fetchone()
        return bool(row and row[0]=='accepted')

    def cleanup(self):
        # Original business text/files live in App; do not retain CDN keys forever.
        cutoff=time.time()-86400
        with self.connect() as db:
            db.execute("UPDATE inbox SET payload='{}' WHERE status='done' AND updated<? AND payload!='{}'",(cutoff,))
            db.execute("UPDATE outbox SET text='[已归档]' WHERE status='accepted' AND updated<?",(time.time()-7*86400,))
            db.execute("DELETE FROM inbox WHERE status='done' AND updated<?",(time.time()-30*86400,))
            # Expired reviews have no execution authority. Keep their audit key,
            # but not a second long-lived copy of every order line and CDN key.
            db.execute("UPDATE approvals SET payload='{}',status='expired' WHERE expires<? AND status IN ('pending','blocked')",
                       (time.time()-7*86400,))
            db.execute("UPDATE approvals SET payload='{}' WHERE created<? AND status='done'",(time.time()-30*86400,))
            db.execute("UPDATE inbox SET payload='{}' WHERE status='error' AND updated<?",(time.time()-30*86400,))
            db.execute("UPDATE inbox SET status='expired',payload='{}' WHERE status='held' AND updated<?",(time.time()-7*86400,))
        with self.connect() as db:
            db.execute('PRAGMA wal_checkpoint(PASSIVE)')

    def business_request(self,binding,row):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO business_requests(id,binding,scope_hash,message,expires) VALUES(?,?,?,?,?)',
                       (row['id'],binding,row['scope_hash'],row['message'],row['expires']))
            saved=dict(db.execute('SELECT * FROM business_requests WHERE id=?',(row['id'],)).fetchone())
            if saved['binding']!=binding or saved['scope_hash']!=row['scope_hash']:raise ValueError('confirmation_scope_conflict')
        self.reply('business-'+row['id'],binding,row['message']+'\n确认编号：'+row['id']+'\n请回复“确认事项 '+row['id']+'”或“暂不处理 '+row['id']+'”。仅确认本条列明的范围，30分钟内有效。')
        with self.connect() as db:
            delivery=db.execute('SELECT status FROM outbox WHERE id=?',('business-'+row['id'],)).fetchone()[0]
        return {**saved,'delivery':delivery}

    def confirm_business(self,binding,code,approve,receipt):
        with self.connect() as db:
            row=db.execute('SELECT * FROM business_requests WHERE id=? AND binding=?',(code,binding)).fetchone()
            if not row or row['expires']<time.time():message='确认编号不存在或已过期，请在 App 待办查看最新请求。'
            elif row['status']!='pending':message='已收到确认；执行前仍会核对版本和订单范围，完成后另行通知。' if row['status']=='approved' else '已记录暂不处理，相关状态保持不变。'
            else:
                db.execute("UPDATE business_requests SET status=?,receipt=? WHERE id=? AND binding=? AND status='pending'",
                           ('approved' if approve else 'declined',receipt,code,binding))
                message='已收到确认；执行前仍会核对版本和订单范围，完成后另行通知。' if approve else '已记录暂不处理，相关状态保持不变。'
        self.reply('business-reply-'+digest([binding,receipt,code]),binding,message)
