"""Owner confirmations record intent; executors still validate current scope.

A message receipt is not execution success. No natural-language text is ever
interpreted as a business mutation by this module.
"""
import hashlib
import json
import secrets
import time
from .store import dumps


def ensure(store):
    with store.connect(write=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS business_confirmations(
            id TEXT PRIMARY KEY,kind TEXT NOT NULL,scope TEXT NOT NULL,scope_hash TEXT NOT NULL,
            message TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',delivery TEXT NOT NULL DEFAULT 'pending',
            receipt TEXT NOT NULL DEFAULT '',created REAL NOT NULL,expires REAL NOT NULL)''')


def create(store,kind,scope,message,ttl=1800):
    if kind not in {'printing_stopped','print_review','business_review'}:raise ValueError('未知确认类型')
    raw=dumps(scope);fingerprint=hashlib.sha256(raw.encode()).hexdigest()
    ensure(store)
    with store.connect(write=True) as db:
        old=db.execute("SELECT id FROM business_confirmations WHERE kind=? AND scope_hash=? AND status='pending' AND expires>?",(kind,fingerprint,time.time())).fetchone()
        if old:return old[0]
        code=secrets.token_hex(5).upper()
        db.execute('INSERT INTO business_confirmations(id,kind,scope,scope_hash,message,created,expires) VALUES(?,?,?,?,?,?,?)',
                   (code,kind,raw,fingerprint,message,time.time(),time.time()+ttl))
    return code


def pending(store):
    ensure(store)
    with store.connect() as db:
        return [dict(r) for r in db.execute("SELECT id,kind,message,status,delivery,created,expires FROM business_confirmations WHERE status IN ('pending','approved','expired') ORDER BY created DESC LIMIT 100")]


def exchange(store,spool,binding,invoke=lambda fn,*args:fn(*args)):
    def load():
        ensure(store)
        with store.connect() as db:return [dict(r) for r in db.execute("SELECT * FROM business_confirmations WHERE status='pending'")]
    def record(row,state):
        with store.connect(write=True) as db:
            if row['expires']<time.time():
                db.execute("UPDATE business_confirmations SET status='expired' WHERE id=? AND status='pending'",(row['id'],));return
            db.execute('UPDATE business_confirmations SET delivery=? WHERE id=?',(state['delivery'],row['id']))
            if state['status'] in {'approved','declined'} and state['scope_hash']==row['scope_hash']:
                db.execute("UPDATE business_confirmations SET status=?,receipt=? WHERE id=? AND status='pending' AND scope_hash=? AND expires>?",
                    (state['status'],state['receipt'],row['id'],row['scope_hash'],time.time()))
    for row in invoke(load):
        state=spool.business_request(binding,row) if row['expires']>=time.time() else {}
        invoke(record,row,state)
