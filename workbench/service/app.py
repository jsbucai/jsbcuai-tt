"""Independent Android API; business sources are read-only, devices are app-owned."""
import json
from email.utils import parsedate_to_datetime

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from core.store import Store, ROOT, dumps
from core.importers import Importer
from core.import_safety import ImportBusy, ImportFailed
from core.sql_queries import Queries
from core.print_jobs import PrintJobs, JobConflict, data_version, print_readiness
from core.queries import inventory_key, QueryCoverageUnavailable
from core.rules import parse_note
from core.snapshots import capture
from core.source_monitor import SourceMonitor
from core.installation import install_routes
from core.wsgjp_reserved import reserved_routes
from core.adjustments import AdjustmentJobs
from core.adjustment_web import WebAdjustmentJobs
from ipaddress import ip_address
from core.auth import Sessions, bearer
from core.dot_readonly import DotReadOnly
from core.api_source import WmsSource
from core.erp_api import ErpClient
from core.update_jobs import UpdateJobs
from core.inventory_ledger import InventoryLedger
from core.product_images import ProductImageCache, ProductImageUnavailable
from core.freight_write_policy import FreightWritePolicy
from core.daily_work import DailyWork, DailyWorkConflict, daily_work_routes
from core.customer_orders import CustomerOrders, customer_order_routes
from core.task_board import TaskBoard, task_board_routes
from core.purchase_unfinished import PurchaseUnfinished, purchase_unfinished_routes
from core.procurement_assistant import ProcurementAssistant, procurement_assistant_routes
from core.purchase_price_bounds import refresh_price_bounds
from core.sales_assistant import SalesAssistant, sales_assistant_routes
from core.sales_history_reference import SalesHistoryReference
from core.order_history_archive import OrderHistoryArchive
from core.order_history_jobs import OrderHistoryJobs
from core.web_operations import WebOperations, web_operation_routes
from core.sales_workspace import install_workspace, cookie_token, csrf, is_sales_request, same_origin, owner_id
from core import purchase_workspace as purchase_web
import hmac


def create_app(config=None, runtime_dir=None):
    app = Flask(__name__)
    # Sales assistant accepts a 10 MiB file plus multipart envelope. Existing
    # routes retain their former 2 MiB request ceiling below.
    app.config.update(AUTO_IMPORT=True, MAX_CONTENT_LENGTH=12 * 1024 * 1024)
    app.config.update(config or {})
    api_mode = app.config.get('SOURCE_MODE') == 'api'
    public_mode = app.config.get('PUBLIC_MODE', False)
    if public_mode and not api_mode:
        raise ValueError('公网服务只支持独立 API 数据源')
    app.register_blueprint(install_routes())
    app.register_blueprint(reserved_routes())
    store = Store(runtime_dir or app.config.get('RUNTIME_DIR') or ROOT / 'runtime' / 'data')
    dot_readonly = DotReadOnly(store, app.config)
    app.register_blueprint(dot_readonly.routes())
    sessions = Sessions(store, app.config.get('ADMIN_PASSWORD_HASH')) if public_mode else None
    if sessions:
        app.register_blueprint(sessions.routes())
    importer, queries = Importer(store), Queries(store)
    jobs = PrintJobs(store, queries)
    ledger = InventoryLedger(store)
    images = ProductImageCache(store)
    with store.connect() as db:
        existing_packaging_ids = {row[0] for row in db.execute(
            "SELECT id FROM daily_work_tasks WHERE role IN ('stock_packing','arrival_packing')"
        )} if db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_work_tasks'").fetchone() else set()
    daily_work = DailyWork(store) if api_mode and public_mode else None
    procurement_assistant = ProcurementAssistant(store, daily_work) if daily_work else None
    sales_assistant = SalesAssistant(store,history_reference=SalesHistoryReference(
        app.config.get('SALES_HISTORY_REFERENCE_PATH'),
        app.config.get('SALES_HISTORY_REFERENCE_SHA256'))) if daily_work else None
    web_operations = WebOperations(store) if daily_work else None
    purchase_workspace = purchase_web.PurchaseWorkspace(store) if daily_work else None
    if purchase_workspace:
        purchase_web.install(app, purchase_workspace, sessions)
    customer_orders = CustomerOrders(store, daily_work) if daily_work else None
    task_board = TaskBoard(store, daily_work, customer_orders) if daily_work else None
    purchase_unfinished = PurchaseUnfinished(store, app.config.get('PURCHASE_SNAPSHOT_DIR'),
        app.config.get('PURCHASE_PREVIEW_DIR')) if api_mode and public_mode else None
    if task_board:
        task_board.purchase = purchase_unfinished
    if purchase_unfinished:
        app.register_blueprint(purchase_unfinished_routes(purchase_unfinished))
    if procurement_assistant:
        app.register_blueprint(procurement_assistant_routes(procurement_assistant))
    if sales_assistant:
        app.register_blueprint(sales_assistant_routes(sales_assistant))
        install_workspace(app,sales_assistant,sessions)
    if web_operations:
        app.register_blueprint(web_operation_routes(web_operations))
    if daily_work:
        # Rebuild workflow facts once for v8 from the already-published local
        # snapshot. This never reads or writes ERP and keeps its original time.
        with store.connect() as db:
            board_revision = db.execute("SELECT value FROM settings WHERE key='task_board_revision'").fetchone()
            has_orders = db.execute("SELECT 1 FROM records WHERE kind='orders' LIMIT 1").fetchone()
        if has_orders and (not board_revision or board_revision[0] != '1'):
            try:
                daily_work.refresh(actor_id='system:board-v8')
                customer_orders.reconcile()
                daily_work.reconcile_product_assignments(actor_id='system:board-v8',
                    existing_task_ids=existing_packaging_ids)
                with store.connect(write=True) as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('task_board_revision','1')")
            except DailyWorkConflict as error:
                app.logger.warning('Task board migration is waiting for valid published data: %s', error.code)
        def staff_changed(actor_id):
            result = daily_work.reconcile_staff_assignments(actor_id=actor_id)
            customer_orders.reconcile()
            task_board._packing_index.refresh()
            return result
        sessions.on_staff_changed = staff_changed
        app.register_blueprint(daily_work_routes(daily_work,
            after_settings=customer_orders.reconcile,
            after_refresh=lambda: refresh_price_bounds(store)))
        app.register_blueprint(customer_order_routes(customer_orders))
        app.register_blueprint(task_board_routes(task_board))
        customer_orders.reconcile()
        # Publish the rebuildable source index before serving the first page.
        # Print changes only read the current state table and do not rebuild it.
        task_board._packing_index.refresh()
        try:
            refresh_price_bounds(store)
        except (ValueError, KeyError) as error:
            app.logger.warning('Purchase price bounds await a complete local snapshot: %s', error)
    sender = app.config.get('ADJUSTMENT_SENDER')
    if sender is None and not app.testing and not api_mode:
        from core.erp_adjustments import ErpAdjustmentSender
        sender = ErpAdjustmentSender(store.runtime).send
    adjustments = WebAdjustmentJobs(store, queries) if api_mode and sender is None else AdjustmentJobs(store, queries, sender)
    def adjustment_available():
        return sender is not None or isinstance(adjustments, WebAdjustmentJobs) and adjustments.enabled()
    monitor = SourceMonitor(store, importer)
    app.extensions['service'] = {'store': store, 'importer': importer, 'queries': queries,
        'jobs': jobs, 'monitor': monitor, 'adjustments': adjustments,
        'inventory_ledger': ledger, 'product_images': images, 'daily_work': daily_work,
        'customer_orders': customer_orders, 'task_board': task_board,
        'purchase_unfinished': purchase_unfinished,
        'procurement_assistant': procurement_assistant,
        'sales_assistant': sales_assistant, 'purchase_workspace': purchase_workspace}
    app.extensions['service']['web_operations'] = web_operations
    updates = erp_client = history_archive = history_jobs = None
    if api_mode:
        write_enabled = app.config.get('LOGISTICS_WRITE_ENABLED') is True
        erp_client = app.config.get('ERP_CLIENT') or ErpClient(
            app.config.get('ERP_VAULT', str(store.runtime / 'secrets' / 'erp.json')),
            allow_logistics=write_enabled)
        freight_sender = app.config.get('LOGISTICS_SENDER')
        if freight_sender is None and write_enabled:
            freight_sender = erp_client.update_logistics
        write_policy = FreightWritePolicy.from_config(
            app.config.get('LOGISTICS_WRITE_MAX_PER_JOB'),
            app.config.get('LOGISTICS_WRITE_ALLOWLIST'),
            app.config.get('LOGISTICS_WRITE_SCOPE'))
        def refresh_work(*args, **kwargs):
            result = daily_work.refresh(*args, **kwargs)
            customer_orders.reconcile()
            task_board._packing_index.refresh()
            refresh_price_bounds(store)
            if purchase_workspace:
                purchase_workspace.rebuild()
            return result
        updates = UpdateJobs(store, app.config.get('API_SOURCE') or WmsSource(
            erp_client, store, ignored_order_ids=app.config.get('IGNORED_ORDER_IDS')),
            freight_sender, write_policy=write_policy,
            after_publish=refresh_work if daily_work else None)
        if public_mode:
            history_archive = OrderHistoryArchive(store)
            history_jobs = OrderHistoryJobs(store, history_archive, erp_client)
            if task_board:
                task_board.history_archive = history_archive
        app.extensions['service'].update(updates=updates, erp_client=erp_client,
            sessions=sessions, order_history_archive=history_archive,
            order_history_jobs=history_jobs)
        if purchase_workspace and not app.testing:
            purchase_workspace.start(updates)
        if daily_work:
            def task_print_access(payload, items, db=None):
                try:
                    by_profile = {}
                    for item in items:
                        by_profile.setdefault(str(item.get('profile_id') or ''), set()).add(
                            str(item.get('order_no') or ''))
                    if payload.get('verified_print_preview') is True:
                        def assert_customer_scope(database):
                            groups = customer_orders._build(database)[0]
                            refs = {(profile, number) for profile, numbers in by_profile.items() for number in numbers}
                            relevant = [group for group in groups.values() if any(
                                (group['profile_id'], line['order_no']) in refs for line in group['lines'])]
                            if any(group['customer_review'] for group in relevant):
                                raise DailyWorkConflict('客户身份待核对，不能领取打印标签', 'customer_identity_review')
                        if db is not None:
                            assert_customer_scope(db)
                        else:
                            with store.connect() as database:
                                assert_customer_scope(database)
                    for profile_id, order_nos in by_profile.items():
                        if payload.get('verified_print_preview') is True:
                            daily_work.assert_reprint_access(g.current_user, order_nos, profile_id, db=db)
                        else:
                            daily_work.assert_print_access(g.current_user, order_nos, profile_id, db=db)
                except DailyWorkConflict as error:
                    raise JobConflict(str(error), error.code) from None
            jobs.order_access_checker = task_print_access
    if not api_mode and app.config['AUTO_IMPORT'] and not store.source_list():
        refresh = None if app.testing else lambda: capture(store.runtime)
        importer.run(refresh=refresh)

    def body():
        content = request.get_json(silent=True)
        if not isinstance(content, dict):
            raise ValueError('请求必须为JSON对象')
        return content

    def ok(**content):
        return jsonify(ok=True, **content)

    def admin_write_required():
        """Protect ERP-wide mutations after employee accounts are enabled."""
        if not public_mode:
            return None
        user = getattr(g, 'current_user', None)
        if isinstance(user, dict) and user.get('is_admin') is True:
            return None
        return jsonify(ok=False, error='admin_required',
            message='只有管理员可以更新订单或处理物流写入结果'), 403

    @app.get('/api/v2/business-confirmations')
    def business_confirmation_list():
        denied=admin_write_required()
        if denied:return denied
        from core.business_confirmations import pending
        return jsonify(items=pending(store))

    def source_updated_at():
        return max((item.get('source_modified_at') or '' for item in store.source_list()), default='')

    def published_profile():
        with store.connect() as db:
            row = db.execute("""SELECT profile FROM sync_batches
                WHERE profile<>'' AND published_at IS NOT NULL
                ORDER BY published_at DESC,rowid DESC LIMIT 1""").fetchone()
            if row:
                return str(row[0])
            row = db.execute("SELECT payload FROM records WHERE kind='inventory' LIMIT 1").fetchone()
        if not row:
            return ''
        try:
            return str(json.loads(row[0]).get('profile_id') or '')
        except (TypeError, ValueError):
            return ''

    @app.before_request
    def read_boundary():
        denied = dot_readonly.boundary()
        if denied is not None:
            return denied
        sales_file_upload = (request.method == 'POST' and
            request.path.startswith('/api/v2/sales-assistant/sessions/') and
            request.path.endswith('/attachments'))
        purchase_file_upload = request.method == 'POST' and request.path == purchase_web.BASE + '/quote-preview'
        if not sales_file_upload and not purchase_file_upload and request.content_length and request.content_length > 2 * 1024 * 1024:
            return jsonify(ok=False, error='request_too_large', message='请求超过大小限制'), 413
        if public_mode and not getattr(g, 'dot_service', False):
            # A new phone must be able to fetch the native client before it
            # can authenticate. Keep the public surface exact and minimal:
            # no prefix matching, directory listing, or caller-selected file.
            public_paths = {
                '/livez', '/app', '/app/download', '/api/v2/auth/login',
                '/sales','/sales/','/api/v2/sales-workspace/login',
                '/purchase','/purchase/','/api/v2/purchase-workspace/login',
            }
            if request.path not in public_paths and not request.path.startswith(('/sales/assets/','/purchase/assets/')):
                token=bearer()
                using_cookie=not token and is_sales_request(request.path)
                using_purchase_cookie=not token and purchase_web.is_request(request.path)
                if using_cookie:token=cookie_token()
                if using_purchase_cookie:token=request.cookies.get(purchase_web.COOKIE,'')
                user = sessions.current(token)
                if not user:
                    return jsonify(ok=False, error='authentication_required', message='请登录，或重新登录已失效的会话'), 401
                if using_cookie:
                    with store.connect() as db:
                        if user['id']!=owner_id(db):
                            return jsonify(ok=False,error='owner_only',message='首版仅本人使用'),403
                    if request.method not in {'GET','HEAD','OPTIONS'} and (not same_origin(app) or
                            not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf(token))):
                        return jsonify(ok=False,error='csrf_required',message='页面验证已失效，请重新登录；输入仍保留'),403
                if using_purchase_cookie and request.method not in {'GET','HEAD','OPTIONS'} and (
                    not purchase_web.same_origin(app) or not hmac.compare_digest(
                        request.headers.get('X-CSRF-Token',''),purchase_web.csrf(token))):
                    return jsonify(ok=False,error='csrf_required',message='页面验证已失效，请重新登录；输入仍保留'),403
                g.current_user = user
        elif not public_mode and request.remote_addr and not ip_address(request.remote_addr).is_private:
            return jsonify(ok=False, error='lan_only', message='首版接口仅接受本机或局域网连接'), 403
        if request.method == 'GET' and not request.path.endswith('/events'):
            g.read_context = store.read_snapshot()
            g.read_context.__enter__()
        if request.method == 'POST' and request.headers.get('Origin') and request.headers['Origin'].rstrip('/') != request.host_url.rstrip('/') and not (is_sales_request(request.path) and same_origin(app)) and not (purchase_web.is_request(request.path) and purchase_web.same_origin(app)):
            return jsonify(ok=False, error='origin_blocked', message='写操作只允许原生App或本服务来源'), 403

    @app.teardown_request
    def end_read(error):
        context = g.pop('read_context', None)
        if context:
            context.__exit__(None, None, None)

    @app.after_request
    def headers(response):
        if 'Cache-Control' not in response.headers:
            response.headers['Cache-Control'] = 'no-store'
        if request.path.startswith(('/sales','/purchase')):
            response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            response.headers['X-Content-Type-Options']='nosniff'
            response.headers['Referrer-Policy']='same-origin'
        response.headers['X-UL-Service'] = 'android-independent-5191'
        return response

    @app.errorhandler(JobConflict)
    def conflict(error):
        return jsonify(ok=False, error=error.code, message=str(error), data_version=data_version(store)), 409

    @app.errorhandler(ImportBusy)
    def busy(error):
        return jsonify(ok=False, error='refresh_in_progress', message=str(error)), 409

    @app.errorhandler(ImportFailed)
    def failed(error):
        return jsonify(ok=False, error='refresh_failed', message=str(error)), 422

    @app.errorhandler(QueryCoverageUnavailable)
    def coverage_unavailable(error):
        return jsonify(ok=False, error='query_coverage_unavailable', message=str(error),
            coverage=error.coverage, data_version=data_version(store)), 422

    @app.errorhandler(ProductImageUnavailable)
    def image_unavailable(error):
        return jsonify(ok=False, error='image_unavailable', message=str(error)), 502

    @app.errorhandler(DailyWorkConflict)
    def daily_work_conflict(error):
        status = 409 if error.code.endswith(('conflict', 'changed', 'reached')) else 403
        return jsonify(ok=False, error=error.code, message=str(error)), status

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    def invalid(error):
        return jsonify(ok=False, error='invalid_request', message=str(error)), 400

    @app.errorhandler(KeyError)
    def missing(error):
        return jsonify(ok=False, error='not_found', message=str(error)), 404

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(ok=False, error=error.name, message=error.description), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception('Android service request failed')
        return jsonify(ok=False, error='internal_error', message='服务处理失败；不会自动发送或补打'), 500

    @app.get('/api/v2/health')
    def health():
        if api_mode:
            return ok(service='ul-android-service', version='1.6.3', data_version=data_version(store),
                profile_id=published_profile(),
                source_updated_at=source_updated_at(), counts=store.counts(), sources=store.source_list(),
                **queries.source_scope(),
                monitor={'mode':'manual_api'}, updates=updates.status(), adjustments=adjustments.status(),
                capabilities={'physical_print':False, 'erp':adjustment_available(), 'inventory_batch_upload':adjustment_available(),
                    'inventory_exact_sku':True, 'manual_inventory_confirmation':True, 'fast_cpcl':True,
                    'native_client_ledger':True, 'wsgjp_api':True, 'source_contract_verified':app.config.get('SOURCE_CONTRACT_VERIFIED') is True,
                    'logistics_write':updates.write_enabled, 'inventory_history':True,
                    'product_images':True,
                    'purchase_unfinished_snapshot':purchase_unfinished is not None,
                    'order_history_manual_read':history_jobs is not None,
                    'order_history_automatic':False,
                    'daily_work':daily_work is not None,
                    'inventory_subset_refresh':callable(getattr(updates.source,'refresh_inventory_subset',None)),
                    'whole_order_print_state':True}, auth_error=erp_client.last_auth_error)
        return ok(service='ul-android-service', version='1.2.2', data_version=data_version(store), source_updated_at=source_updated_at(), counts=store.counts(), sources=store.source_list(), monitor=monitor.status(), adjustments=adjustments.status(), capabilities={'physical_print': False, 'erp': True, 'inventory_batch_upload': True, 'inventory_exact_sku': True, 'manual_inventory_confirmation': True, 'fast_cpcl': True, 'native_client_ledger': True, 'wsgjp_api': False, 'wsgjp_reserved_endpoints': True})

    @app.get('/livez')
    def livez():
        return ok(service='ul-android-service')

    @app.post('/api/v2/update-jobs')
    def enqueue_update():
        if not updates:
            return jsonify(ok=False,error='capability_unavailable',message='当前服务未启用 API 更新'),409
        value = body()
        mode = value.get('mode','sync')
        if mode != 'inventory_subset':
            denied = admin_write_required()
            if denied:
                return denied
        subset_request = value.get('request')
        if mode == 'inventory_subset' and subset_request is None:
            subset_request = {'product_ids': value.get('product_ids', []),
                'identities': value.get('identities', [])}
        job = updates.enqueue(value.get('id'), mode, subset_request) if mode == 'inventory_subset' \
            else updates.enqueue(value.get('id'), mode)
        return ok(job=job),202

    @app.get('/api/v2/update-jobs/latest')
    def latest_update():
        return ok(job=updates.latest() if updates else None)

    @app.get('/api/v2/update-jobs/history')
    def update_history():
        denied = admin_write_required()
        if denied:
            return denied
        if not updates:
            return ok(jobs=[])
        return ok(jobs=updates.history(request.args.get('limit',50),
                                      request.args.get('offset',0)))

    @app.get('/api/v2/update-jobs/<identity>')
    def get_update(identity):
        if not updates: raise KeyError('未启用 API 更新')
        return ok(job=updates.get(identity))

    @app.get('/api/v2/order-history/coverage')
    def order_history_coverage():
        denied = admin_write_required()
        if denied:
            return denied
        if history_archive is None:
            return jsonify(ok=False,error='capability_unavailable',
                message='当前服务未启用历史订单查询'),409
        year = request.args.get('year')
        return ok(coverage=history_archive.coverage(
            request.args.get('profile_id'), year=int(year) if year is not None else None))

    @app.get('/api/v2/order-history/lines')
    def order_history_lines():
        denied = admin_write_required()
        if denied:
            return denied
        if history_archive is None:
            return jsonify(ok=False,error='capability_unavailable',
                message='当前服务未启用历史订单查询'),409
        return ok(**history_archive.query_lines(
            request.args.get('profile_id'), product_code=request.args.get('product_code'),
            order_no=request.args.get('order_no'), limit=request.args.get('limit',50),
            offset=request.args.get('offset',0)))

    @app.post('/api/v2/order-history/jobs')
    def enqueue_order_history():
        denied = admin_write_required()
        if denied:
            return denied
        if history_jobs is None:
            return jsonify(ok=False,error='capability_unavailable',
                message='当前服务未启用历史订单查询'),409
        value = body()
        return ok(job=history_jobs.enqueue(value.get('id'), value.get('profile_id'),
            value.get('month'), rescan=value.get('rescan',False))),202

    @app.get('/api/v2/order-history/jobs')
    def order_history_jobs_list():
        denied = admin_write_required()
        if denied:
            return denied
        if history_jobs is None:
            return jsonify(ok=False,error='capability_unavailable',
                message='当前服务未启用历史订单查询'),409
        return ok(jobs=history_jobs.history(request.args.get('limit',50),
                                           request.args.get('offset',0)))

    @app.get('/api/v2/order-history/jobs/<identity>')
    def order_history_job(identity):
        denied = admin_write_required()
        if denied:
            return denied
        if history_jobs is None:
            return jsonify(ok=False,error='capability_unavailable',
                message='当前服务未启用历史订单查询'),409
        return ok(job=history_jobs.get(identity))

    @app.post('/api/v2/freight-attempts/<identity>/resolve')
    def resolve_freight(identity):
        if not updates:
            return jsonify(ok=False,error='capability_unavailable',message='当前服务未启用 API 更新'),409
        denied = admin_write_required()
        if denied:
            return denied
        value = body()
        return ok(attempt=updates.resolve_attempt(identity, value.get('resolution'), value.get('note')))

    @app.get('/api/v2/data/sources')
    def sources():
        return ok(sources=store.source_list(), data_version=data_version(store), source_updated_at=source_updated_at(), monitor=monitor.status())

    @app.post('/api/v2/data/refresh')
    def refresh():
        if updates:
            denied = admin_write_required()
            if denied:
                return denied
            value=body()
            return ok(job=updates.enqueue(value.get('id'),value.get('mode','sync'))),202
        if app.testing:
            results = importer.run()
        else:
            results = importer.run(refresh=lambda: capture(store.runtime))
        return ok(sources=results, data_version=data_version(store), message='已从原业务文件只读更新，旧服务及原文件未修改')

    @app.get('/api/v2/orders')
    def orders():
        result = queries.orders(request.args)
        cache = {}
        for item in result['items']:
            key = inventory_key(item)
            if key not in cache:
                cache[key] = queries.inventory_lookup(item.get('sku', ''), item)
            item['inventory'] = cache[key]
            spec = item.get('spec') or item['inventory'].get('spec', '')
            standard = item.get('standard_no') or item['inventory'].get('standard_no', '')
            item['specification'] = '  '.join(part for part in
                (standard if standard and not str(spec).startswith(str(standard)) else '', spec) if part)
            issues = item.get('calculation_issues', item.get('issues', []))
            item['calculation_issues'] = [{**issue, 'message': issue.get('message') or issue.get('reason', '')}
                if isinstance(issue, dict) else {'message': str(issue)} for issue in issues]
            item['issues'] = item['calculation_issues']
            item.setdefault('calculation_status', 'ready' if item.get('logistics_company') else 'blocked')
            item['printable'], item['print_block_reason'] = print_readiness('order', item)
        return ok(**result, data_version=data_version(store), source_updated_at=source_updated_at(), **queries.source_scope())

    @app.get('/api/v2/inventory')
    def inventory():
        result = queries.inventory(request.args)
        for item in result['items']:
            item['printable'], item['print_block_reason'] = print_readiness('inventory', item)
        return ok(**result, data_version=data_version(store), source_updated_at=source_updated_at())

    @app.get('/api/v2/inventory/<path:record_id>/history')
    def inventory_history(record_id):
        options = {}
        if 'days' in request.args:
            options = {'days':request.args.get('days'),
                       'limit':request.args.get('limit',30),
                       'offset':request.args.get('offset',0)}
        return ok(**ledger.history(record_id,**options))

    @app.get('/api/v2/products/<path:product_id>/images')
    def product_images(product_id):
        return ok(**images.list_for_product(product_id))

    @app.get('/api/v2/product-images/<path:image_id>/content')
    def product_image_content(image_id):
        value = images.content(image_id)
        last_modified = None
        if value.last_modified:
            try:
                last_modified = parsedate_to_datetime(value.last_modified)
            except (TypeError, ValueError, OverflowError):
                pass
        etag = value.etag.strip()
        if etag.startswith('W/'):
            etag = etag[2:]
        etag = etag.strip('"')
        response = send_file(value.path, mimetype=value.mime_type, conditional=True,
            etag=etag or True, last_modified=last_modified)
        response.headers['Cache-Control'] = 'private, max-age=86400'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/api/v2/purchases')
    def purchases():
        return ok(**queries.purchases(request.args), data_version=data_version(store), source_updated_at=source_updated_at())

    @app.post('/api/v2/print/jobs/preview')
    def preview_print_jobs():
        return ok(**jobs.preview(body(), actor=getattr(g, 'current_user', None)))

    @app.post('/api/v2/admin/print-state-corrections/preview')
    def preview_print_correction():
        denied = admin_write_required()
        if denied:
            return denied
        from core.print_corrections import preview
        payload = body()
        return ok(**preview(store, getattr(g, 'current_user', None), payload.get('scope', 'unfulfilled'), payload.get('order_refs')))

    @app.post('/api/v2/admin/print-state-corrections/apply')
    def apply_print_correction():
        denied = admin_write_required()
        if denied:
            return denied
        from core.print_corrections import apply
        result = apply(store, getattr(g, 'current_user', None), body())
        if daily_work:
            try:
                refreshed = daily_work.refresh(actor_id=str(g.current_user['id']))
                result['task_refresh'] = {'ok': True, 'task_version': refreshed.get('task_version')}
            except Exception:
                app.logger.exception('task refresh failed after print correction')
                result['task_refresh'] = {'ok': False, 'message': '纠正已完成，任务刷新待补跑'}
        return ok(**result)

    @app.post('/api/v2/admin/printing-maintenance')
    def printing_maintenance():
        denied = admin_write_required()
        if denied:
            return denied
        payload = body()
        if type(payload.get('paused')) is not bool:
            raise ValueError('需明确提供暂停或恢复状态')
        with store.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('printing_maintenance',?)",
                       (dumps(payload['paused']),))
        return ok(paused=payload['paused'])

    @app.post('/api/v2/print/jobs')
    def prepare():
        return ok(**jobs.prepare(body(), actor=getattr(g, 'current_user', None)))

    @app.get('/api/v2/print/jobs/<identity>')
    def job(identity):
        return ok(**jobs.get(identity))

    @app.post('/api/v2/print/jobs/<identity>/claim')
    def claim(identity):
        return ok(**jobs.claim(identity, body(), actor=getattr(g, 'current_user', None)))

    @app.post('/api/v2/print/jobs/<identity>/cancel')
    def cancel_print_job(identity):
        return ok(**jobs.cancel(identity, body()))

    @app.post('/api/v2/print/jobs/<identity>/recover')
    def recover_print_job(identity):
        return ok(**jobs.recover(identity, body()))

    @app.post('/api/v2/print/jobs/<identity>/result')
    def result(identity):
        return ok(**jobs.result(identity, body()))

    @app.post('/api/v2/orders/<path:order_no>/print-state/reset')
    def reset_order_print_state(order_no):
        payload = body()
        reset_guard = None
        if daily_work:
            def reset_guard(db, _items):
                daily_work.assert_print_reset_access(
                    g.current_user, [order_no], str(payload.get('profile_id') or ''), db=db)
        result = jobs.reset_order_state(order_no, payload, access_checker=reset_guard,
                                        actor=getattr(g, 'current_user', None))
        if daily_work:
            try:
                refreshed = daily_work.refresh(actor_id=str(g.current_user.get('id') or 'system'))
                result['task_refresh'] = {
                    'ok': True,
                    'status': 'succeeded',
                    'task_version': refreshed.get('task_version'),
                    'created': refreshed.get('created', 0),
                    'updated': refreshed.get('updated', 0),
                    'condition_resolved': refreshed.get('condition_resolved', 0),
                }
            except Exception as error:  # reset is committed; task repair is safely retryable
                app.logger.exception('daily-work refresh failed after print-state reset')
                result['task_refresh'] = {
                    'ok': False,
                    'status': 'failed',
                    'error': (error.code if isinstance(error, DailyWorkConflict)
                              else 'task_refresh_failed'),
                    'message': '打印状态已重置；每日任务刷新失败，请稍后重新更新任务',
                }
        return ok(**result)

    @app.post('/api/v2/adjustment-batches')
    def enqueue_adjustments():
        with store.connect() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='restoration_hold' AND value='true'").fetchone():
                raise JobConflict('恢复后库存写入保持暂停','restoration_hold')
        if api_mode and not adjustment_available():
            return jsonify(ok=False,error='capability_unavailable',message='N150 报损报溢网页通道暂未启用，草稿已保留，不会创建单据'),409
        return ok(batch=adjustments.enqueue(body(), actor=getattr(g, 'current_user', None))
                  if isinstance(adjustments, WebAdjustmentJobs) else adjustments.enqueue(body()))

    @app.get('/api/v2/adjustment-batches')
    def list_adjustments():
        return ok(batches=adjustments.list_for(request.args.get('client_id'), getattr(g, 'current_user', None))
                  if isinstance(adjustments, WebAdjustmentJobs) else adjustments.list(request.args.get('client_id')))

    @app.get('/api/v2/adjustment-batches/<identity>')
    def get_adjustments(identity):
        if isinstance(adjustments, WebAdjustmentJobs): adjustments.authorize(identity, getattr(g, 'current_user', None))
        return ok(batch=adjustments.get(identity, request.args.get('client_id')))

    @app.post('/api/v2/adjustment-batches/<identity>/retry')
    def retry_adjustments(identity):
        if isinstance(adjustments, WebAdjustmentJobs):
            adjustments.authorize(identity, getattr(g, 'current_user', None))
            if not adjustments.enabled(): raise JobConflict('N150 报损报溢已暂停', 'capability_unavailable')
        with store.connect() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='restoration_hold' AND value='true'").fetchone():
                raise JobConflict('恢复后库存写入保持暂停','restoration_hold')
        return ok(batch=adjustments.retry(identity, body().get('client_id')))

    @app.post('/api/v2/adjustment-batches/<identity>/confirm')
    def confirm_adjustments(identity):
        if isinstance(adjustments, WebAdjustmentJobs): adjustments.authorize(identity, getattr(g, 'current_user', None))
        return ok(batch=adjustments.confirm(identity, body()))

    return app
