"""Independent Android API with explicitly submitted inventory batch worker."""
import argparse
import json
import os
from pathlib import Path
from waitress import serve
from app import create_app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', default=5191, type=int)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.port in {5088, 5188, 5190, 5099}:
        parser.error('Refusing a port reserved for existing services')
    source_mode = os.environ.get('UL_SOURCE_MODE', 'api')
    public_mode = os.environ.get('UL_PUBLIC_MODE', '1') == '1'
    password_file = os.environ.get('UL_ADMIN_PASSWORD_HASH_FILE')
    password_hash = Path(password_file).read_text().strip() if password_file else None
    ignored_order_ids = [value.strip() for value in
                         os.environ.get('UL_IGNORED_ORDER_IDS', '').split(',') if value.strip()]
    app = create_app({'SOURCE_MODE':source_mode,'PUBLIC_MODE':public_mode,
                      'LOGISTICS_WRITE_ENABLED':os.environ.get('UL_LOGISTICS_WRITE_ENABLED','0') == '1',
                      'LOGISTICS_WRITE_SCOPE':os.environ.get('UL_LOGISTICS_WRITE_SCOPE',''),
                      'LOGISTICS_WRITE_MAX_PER_JOB':os.environ.get('UL_LOGISTICS_WRITE_MAX_PER_JOB','0'),
                      'LOGISTICS_WRITE_ALLOWLIST':os.environ.get('UL_LOGISTICS_WRITE_ALLOWLIST',''),
                      'ADMIN_PASSWORD_HASH':password_hash,
                      'IGNORED_ORDER_IDS':ignored_order_ids,
                      'PURCHASE_SNAPSHOT_DIR':os.environ.get('UL_PURCHASE_SNAPSHOT_DIR') or None,
                      'PURCHASE_PREVIEW_DIR':os.environ.get('UL_PURCHASE_PREVIEW_DIR') or None,
                      'SALES_HISTORY_REFERENCE_PATH':os.environ.get('UL_SALES_HISTORY_REFERENCE_PATH') or None,
                      'SALES_HISTORY_REFERENCE_SHA256':os.environ.get('UL_SALES_HISTORY_REFERENCE_SHA256') or None,
                      'ERP_VAULT':os.environ.get('UL_ERP_VAULT','runtime/data/secrets/erp.json')})
    if args.prepare_only:
        print(json.dumps(app.extensions['service']['store'].counts(), ensure_ascii=False))
    else:
        runtime = app.extensions['service']['store'].runtime
        (runtime / 'server.json').write_text(json.dumps({'pid': os.getpid(), 'port': args.port, 'service': 'ul-android-service', 'script': str(Path(__file__).resolve())}), encoding='utf-8')
        services = app.extensions['service']
        updates = services.get('updates')
        history_jobs = services.get('order_history_jobs')
        if updates:
            if not updates.start():
                raise RuntimeError('Another update worker already owns this runtime')
            services['erp_client'].start_auth_maintenance()
        else:
            services['monitor'].start()
        if history_jobs and not history_jobs.start():
            if updates:
                services['erp_client'].stop_event.set()
                updates.stop()
            raise RuntimeError('Another order-history worker already owns this runtime')
        try:
            if services['adjustments'].sender is not None and not services['adjustments'].start():
                raise RuntimeError('Another inventory batch worker already owns this runtime')
            serve(app, host=args.host, port=args.port, threads=6)
        finally:
            if history_jobs:
                history_jobs.stop()
            if updates:
                services['erp_client'].stop_event.set()
                updates.stop()
            services['adjustments'].stop()
