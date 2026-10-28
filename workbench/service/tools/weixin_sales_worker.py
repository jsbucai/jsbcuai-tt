"""Separate low-priority owner sales bridge; never starts a browser or model.

All analysis, product creation and order import run on their existing queues.
The Weixin process keeps Bot credentials; this worker only sees owner-filtered
input and cannot send messages to arbitrary recipients.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import threading
import time

from core.sales_assistant import SalesAssistant
from core.sales_history_reference import SalesHistoryReference
from core.store import Store
from core.weixin_sales_bridge import SalesBridge
from tools.sales_codex_worker import _database_identity,FORMAL_HISTORY,FORMAL_HISTORY_SHA
from tools.weixin_sales_media import download
from tools.weixin_sales_spool import Spool


def configuration(path):
    if path.is_symlink() or path.stat().st_mode&0o077 or path.stat().st_uid!=os.getuid():
        raise ValueError('unsafe_weixin_sales_config')
    data=json.loads(path.read_text())
    if data.get('enabled') is not True:raise ValueError('weixin_sales_disabled')
    if not all(isinstance(data.get(k),str) and data[k] for k in ('binding','actor_id','profile_id','runtime','spool')):
        raise ValueError('invalid_weixin_sales_config')
    return data


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('/etc/ul-weixin-sales/config.json'))
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    config=configuration(args.config);spool=Spool(config['spool'])
    stop=threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,lambda *_:stop.set())
    with (spool.root/'.worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        dbpath=Path(config['runtime'])/'workbench.sqlite3';info=dbpath.stat()
        def invoke(fn,*args,**kwargs):
            with _database_identity(info.st_uid,info.st_gid):return fn(*args,**kwargs)
        store=invoke(Store,config['runtime'])
        reference=invoke(SalesHistoryReference,config.get('history_path',FORMAL_HISTORY),
                         config.get('history_sha256',FORMAL_HISTORY_SHA))
        engine=invoke(SalesAssistant,store,history_reference=reference)
        # File download/decrypt + spreadsheet parsing share the existing heavy lane.
        def bounded_download(item):return download(item)
        bridge=SalesBridge(engine,spool,actor_id=config['actor_id'],profile_id=config['profile_id'],
                           binding=config['binding'],download=bounded_download,invoke=invoke)
        cleaned=0
        while not stop.is_set():
            try:
                from core.business_confirmations import exchange
                exchange(store,spool,config['binding'],invoke)
                # Keep ingestion, Excel parsing and background model/browser work sequential.
                with (store.runtime/'.web-browser-lane.lock').open('a') as lane:
                    try:fcntl.flock(lane,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        spool.set('worker_status',{'status':'waiting_for_shared_lane','checked_at':time.time()})
                        if args.once:return
                        stop.wait(3);continue
                    count=0
                    while count<10 and bridge.process_one():count+=1
                    bridge.tick()
                spool.set('worker_status',{'status':'ok','checked_at':time.time()})
                if time.time()-cleaned>3600:spool.cleanup();cleaned=time.time()
            except Exception as error:
                # Do not leak original messages, CDN keys, passwords or exception bodies.
                spool.set('worker_status',{'status':'failed','error_type':type(error).__name__,'checked_at':time.time()})
                if args.once:raise SystemExit(1)
                stop.wait(20)
            if args.once:return
            stop.wait(3)


if __name__=='__main__':main()
