"""Loopback-only Android test server with synthetic data and no ERP connection.

Run with PYTHONPATH=service using the development Python environment. The
password below belongs only to this isolated test account, never production.
"""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
from werkzeug.security import generate_password_hash
from app import create_app
from core.store import dumps, timestamp
from core.sales_history_matching import file_sha256, import_history_snapshot

RUNTIME = Path(__file__).resolve().parents[1] / 'service/runtime/emulator-20261007'
RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
app = create_app({'AUTO_IMPORT': False, 'SOURCE_MODE': 'api', 'PUBLIC_MODE': True,
    'ADMIN_PASSWORD_HASH': generate_password_hash('emulator-test-only'),
    'ERP_CLIENT': SimpleNamespace(last_auth_error=None), 'API_SOURCE': object()}, runtime_dir=RUNTIME)
store = app.extensions['service']['store']
with store.connect(write=True) as db:
    now = timestamp()
    db.execute("UPDATE staff_users SET roles=? WHERE id='admin'", (dumps(['sales','purchase']),))
    for key,value in {'data_version': 'emulator-v1', 'data_source_updated_at': now,
                      'restoration_hold': False}.items():
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key,dumps(value)))
    db.execute("""INSERT OR IGNORE INTO sales_customer_owners
        (profile_id,customer_name,owner_id,status,source,updated_at)
        VALUES ('P','测试客户甲','admin','assigned','customer_confirmed',?)""", (now,))
    product = {'profile_id':'P','warehouse_id':'08390','product_id':'product-test-1',
        'warehouse_name':'江苏优螺',
        'sku_id':'sku-test-1','base_unit':'1','unit_name':'支','sku':'LS1000',
        'product_name':'模拟器测试螺栓','spec':'M6*30','stock_quantity':'20',
        'available_stock':'20','quantity_basis':'base_unit'}
    db.execute("INSERT OR REPLACE INTO records(kind,id,sku,payload,keywords) VALUES ('inventory',?,?,?,?)",
               ('emulator-product','LS1000',dumps(product),'ls1000模拟器测试螺栓m630'))
    db.execute("INSERT OR IGNORE INTO purchase_whitelist_sources VALUES (?,?,?,?,?,?)",
               ('emulator-source','e'*64,now,385,0,385))
    for index in range(385):
        db.execute("""INSERT OR IGNORE INTO purchase_whitelist_entries
            (id,profile_id,erp_code,platform,platform_sku,product_name,spec_check,model_check,
             enabled,review_reason,source_id,source_row,imported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (f'emulator-mapping-{index}','P',
            f'LS{1000+index}','淘钉钉',f'fixture-{index}','模拟器候选商品','M6*30','8.8级',
            0,'待平台实时复核','emulator-source',index+2,now))

# One plainly synthetic old-summary reference exercises manual candidate
# selection. It is unrelated to the private NAS index or any ERP customer.
reference=RUNTIME/'synthetic-sales-reference.sqlite3'
if not reference.exists():
    history={'order_row_id':'fixture-history-1','customer_name':'测试客户甲',
        'customer_code':'fixture-customer','erp_code':'LS1000','order_date':'2026-07-01',
        'source_file':'synthetic-old-order.xls','source_excel_row':'2',
        'customer_order_no':'TEST-OLD-PO','project_no':'-','order_line':'1',
        'customer_part_no':'PART-TEST-1','customer_product_name':'模拟器测试螺栓',
        'customer_standard_spec':'M6*30','customer_grade':'-','customer_surface':'-',
        'customer_quantity_text':'10','customer_quantity_num':'10',
        'is_usable_for_history':'是','remark_system_code_matches_erp':'是',
        'raw_remark':'TEST-OLD-PO#-#1#PART-TEST-1#模拟器测试螺栓#M6*30#-#-#10#LS1000'}
    with sqlite3.connect(reference) as db:
        db.execute('CREATE TABLE historical_orders('+','.join('"'+c+'" TEXT' for c in history)+')')
        db.execute('INSERT INTO historical_orders VALUES ('+','.join('?' for _ in history)+')',
                   list(history.values()))
    reference.chmod(0o600)
with store.connect(write=True) as db:
    import_history_snapshot(db,reference,'P',file_sha256(reference))

print('Isolated emulator fixture: https://127.0.0.1:5193; synthetic data; no ERP client', flush=True)
app.run(host='127.0.0.1', port=5193, threaded=True, use_reloader=False,
        ssl_context=(str(RUNTIME/'tls/cert.pem'),str(RUNTIME/'tls/key.pem')))
