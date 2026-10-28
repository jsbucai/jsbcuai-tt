"""Bounded API-only dictionary refresh, outside interactive request handling."""
import argparse
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.store import Store,dumps
from core.erp_api import ErpClient
from core.sales_customer_directory import ensure,needed,refresh
from tools.sales_codex_worker import _database_identity,_ServiceIdentityStore

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',required=True);p.add_argument('--vault',required=True)
    p.add_argument('--profile',required=True);p.add_argument('--force',action='store_true');args=p.parse_args()
    info=(Path(args.runtime)/'workbench.sqlite3').stat()
    with _database_identity(info.st_uid,info.st_gid):store=Store(args.runtime)
    store=_ServiceIdentityStore(store)
    with store.connect(write=True) as db:
        ensure(db);run=args.force or needed(db,args.profile,datetime.now(ZoneInfo('Asia/Shanghai')))
    if run:
        try:print(dumps(refresh(store,ErpClient(args.vault),args.profile)))
        except Exception:
            print(dumps({'ok':False,'message':'ERP 客户目录刷新失败，保留上次完整快照'}));sys.exit(1)
