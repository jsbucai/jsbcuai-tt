import json
import sqlite3
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from .import_safety import snapshot_directory

ROOT = Path(__file__).resolve().parents[1]


def timestamp():
    return datetime.now().isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def guarded_runtime(value=None):
    allowed = (ROOT / "runtime").resolve()
    if not allowed.is_relative_to(ROOT.resolve()):
        raise ValueError("runtime 本身不能链接到开发项目之外")
    runtime = Path(value or allowed).resolve()
    if not runtime.is_relative_to(allowed):
        raise ValueError("数据目录必须在独立 Android service/runtime 内，拒绝生产及其他服务写路径")
    runtime.mkdir(parents=True, exist_ok=True)
    for filename in ("workbench.sqlite3", "workbench.sqlite3-wal", "workbench.sqlite3-shm"):
        if not (runtime / filename).resolve().is_relative_to(allowed):
            raise ValueError("数据库路径不得通过链接指向开发目录外")
    return runtime


class Store:
    def __init__(self, runtime=None):
        self.runtime = guarded_runtime(runtime)
        self.path = self.runtime / "workbench.sqlite3"
        existing_database = self.path.exists()
        self.snapshot_root = self.runtime / "snapshots"
        if not self.snapshot_root.resolve().is_relative_to(self.runtime):
            raise ValueError("快照目录不能链接到开发数据目录之外")
        self.snapshot_root.mkdir(exist_ok=True)
        self._reader = ContextVar(f"workbench_reader_{id(self)}", default=None)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,
                    sku TEXT,buyer TEXT,note TEXT,keywords TEXT,record_date TEXT,quantity REAL,amount REAL,logistics TEXT,
                    PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS record_sku ON records(kind,sku);
                CREATE INDEX IF NOT EXISTS record_date ON records(kind,record_date);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,payload TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS labels(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS sources(kind TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS print_jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,frozen TEXT NOT NULL,
                    metadata TEXT NOT NULL,idempotency_key TEXT UNIQUE,claim_token_hash TEXT);
            """)
        self._migrate(existing_database)

    def _migrate(self, existing_database):
        """Additive migrations; take a consistent private backup before upgrade."""
        import fcntl
        with (self.runtime / '.schema.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.connect() as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version >= 11:
                    return
                if existing_database:
                    directory = self.runtime / 'migration-backups'
                    directory.mkdir(mode=0o700, exist_ok=True)
                    target = directory / (f'before-v{version + 1}-' + uuid.uuid4().hex + '.sqlite3')
                    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                    with sqlite3.connect(target) as backup:
                        db.backup(backup)
                        if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('数据库升级前备份校验失败')
            with self.connect(write=True) as db:
                if version < 1:
                    statements = [
                    '''CREATE TABLE IF NOT EXISTS sync_batches(job_id TEXT PRIMARY KEY, profile TEXT,
                        status TEXT, source_updated_at TEXT, data_version TEXT, counts TEXT,
                        snapshot TEXT, created_at TEXT, published_at TEXT)''',
                    '''CREATE TABLE IF NOT EXISTS sync_issues(job_id TEXT, order_no TEXT, payload TEXT)''',
                    '''CREATE INDEX IF NOT EXISTS sync_issue_job ON sync_issues(job_id,order_no)''',
                    '''CREATE TABLE IF NOT EXISTS freight_attempt_scopes(attempt_id TEXT PRIMARY KEY,
                        inventory_keys TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS freight_resolution_audit(id TEXT PRIMARY KEY,
                        attempt_id TEXT, resolution TEXT, note TEXT, actor TEXT, created_at TEXT)''',
                    ]
                    for statement in statements:
                        db.execute(statement)
                    db.execute('PRAGMA user_version=1')
                if version < 2:
                    db.execute('''CREATE TABLE IF NOT EXISTS erp_reference_cache(
                        kind TEXT PRIMARY KEY, profile TEXT NOT NULL, payload TEXT NOT NULL,
                        fetched_at TEXT NOT NULL, source_hash TEXT NOT NULL)''')
                    db.execute('PRAGMA user_version=2')
                if version < 3:
                    db.execute('''CREATE TABLE IF NOT EXISTS erp_wms_cache(
                        profile TEXT NOT NULL, tradeid TEXT NOT NULL, payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL, PRIMARY KEY(profile,tradeid))''')
                    db.execute('''CREATE TABLE IF NOT EXISTS erp_sync_watermarks(
                        kind TEXT NOT NULL, profile TEXT NOT NULL, payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL, PRIMARY KEY(kind,profile))''')
                    db.execute('''CREATE TABLE IF NOT EXISTS erp_order_date_cache(
                        profile TEXT NOT NULL, tradeid TEXT NOT NULL, order_date TEXT NOT NULL,
                        source TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY(profile,tradeid))''')
                    db.execute('PRAGMA user_version=3')
                if version < 4:
                    db.executescript('''
                        CREATE TABLE IF NOT EXISTS inventory_movements(
                            event_id TEXT PRIMARY KEY,
                            profile_id TEXT NOT NULL,
                            warehouse_id TEXT NOT NULL,
                            product_id TEXT NOT NULL,
                            sku_id TEXT NOT NULL,
                            base_unit TEXT NOT NULL,
                            source_bill_id TEXT NOT NULL,
                            source_line_id TEXT NOT NULL,
                            document_no TEXT NOT NULL,
                            document_type_code TEXT NOT NULL,
                            document_type TEXT NOT NULL,
                            business_date TEXT NOT NULL,
                            posted_at TEXT NOT NULL,
                            party TEXT NOT NULL DEFAULT '',
                            warehouse_name TEXT NOT NULL DEFAULT '',
                            raw_quantity TEXT NOT NULL,
                            signed_quantity TEXT,
                            inout INTEGER,
                            direction TEXT NOT NULL DEFAULT '',
                            redold INTEGER NOT NULL DEFAULT 0,
                            redword INTEGER NOT NULL DEFAULT 0,
                            summary TEXT NOT NULL DEFAULT '',
                            comment TEXT NOT NULL DEFAULT '',
                            source_hash TEXT NOT NULL,
                            semantics_verified INTEGER NOT NULL DEFAULT 0,
                            sync_token TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(profile_id,source_bill_id,source_line_id,warehouse_id)
                        );
                        CREATE INDEX IF NOT EXISTS inventory_movement_identity
                            ON inventory_movements(profile_id,warehouse_id,product_id,sku_id,base_unit,posted_at);
                        CREATE INDEX IF NOT EXISTS inventory_movement_window
                            ON inventory_movements(profile_id,warehouse_id,posted_at);
                        CREATE TABLE IF NOT EXISTS inventory_snapshots(
                            profile_id TEXT NOT NULL,
                            warehouse_id TEXT NOT NULL,
                            product_id TEXT NOT NULL,
                            sku_id TEXT NOT NULL,
                            base_unit TEXT NOT NULL,
                            physical_quantity TEXT NOT NULL,
                            saleable_quantity TEXT NOT NULL,
                            as_of TEXT NOT NULL,
                            data_version TEXT NOT NULL,
                            reconciliation_status TEXT NOT NULL,
                            coverage_from TEXT NOT NULL,
                            coverage_to TEXT NOT NULL,
                            reason TEXT NOT NULL DEFAULT '',
                            PRIMARY KEY(profile_id,warehouse_id,product_id,sku_id,base_unit)
                        );
                        CREATE TABLE IF NOT EXISTS inventory_sync_watermarks(
                            profile_id TEXT NOT NULL,
                            warehouse_id TEXT NOT NULL,
                            source TEXT NOT NULL,
                            last_success_at TEXT,
                            coverage_from TEXT NOT NULL,
                            coverage_to TEXT NOT NULL,
                            last_full_at TEXT,
                            status TEXT NOT NULL,
                            payload TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY(profile_id,warehouse_id,source)
                        );
                        CREATE TABLE IF NOT EXISTS product_images(
                            image_id TEXT PRIMARY KEY,
                            profile_id TEXT NOT NULL,
                            product_id TEXT NOT NULL,
                            source_url TEXT NOT NULL,
                            mime_type TEXT NOT NULL DEFAULT '',
                            etag TEXT NOT NULL DEFAULT '',
                            last_modified TEXT NOT NULL DEFAULT '',
                            content_sha256 TEXT NOT NULL DEFAULT '',
                            content_length INTEGER NOT NULL DEFAULT 0,
                            width INTEGER,
                            height INTEGER,
                            cache_path TEXT NOT NULL DEFAULT '',
                            status TEXT NOT NULL DEFAULT 'pending',
                            last_seen_at TEXT NOT NULL,
                            fetched_at TEXT,
                            error TEXT NOT NULL DEFAULT '',
                            UNIQUE(profile_id,product_id)
                        );
                        CREATE INDEX IF NOT EXISTS product_image_product
                            ON product_images(profile_id,product_id);
                    ''')
                    db.execute('PRAGMA user_version=4')
                if version < 5:
                    # Staff authentication and daily-work tables are installed
                    # by their feature owners immediately after Store is
                    # constructed. Advancing here is deliberate: it makes the
                    # consistent migration backup happen before either owner
                    # can add its schema to an existing production database.
                    db.execute('PRAGMA user_version=5')
                if version < 6:
                    columns = {row['name'] for row in db.execute('PRAGMA table_info(print_jobs)')}
                    if 'prepare_idempotency_key' not in columns:
                        db.execute('ALTER TABLE print_jobs ADD COLUMN prepare_idempotency_key TEXT')
                    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS print_job_prepare_key
                        ON print_jobs(prepare_idempotency_key)
                        WHERE prepare_idempotency_key IS NOT NULL''')
                    db.execute('PRAGMA user_version=6')
                if version < 7:
                    db.executescript('''
                        CREATE TABLE IF NOT EXISTS customer_order_followups(
                            group_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                            warehouse_id TEXT NOT NULL, customer_order_no TEXT NOT NULL,
                            assignee_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'todo',
                            next_follow_up_date TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
                            source_data_version TEXT NOT NULL DEFAULT '',
                            source_fingerprint TEXT NOT NULL DEFAULT '',
                            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS customer_order_followup_events(
                            id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                            idempotency_key TEXT NOT NULL, actor_id TEXT NOT NULL,
                            note TEXT NOT NULL, waiting_for TEXT NOT NULL DEFAULT '',
                            next_follow_up_date TEXT NOT NULL, created_at TEXT NOT NULL,
                            UNIQUE(group_id,idempotency_key));
                        CREATE INDEX IF NOT EXISTS customer_order_followup_owner
                            ON customer_order_followups(assignee_id,status,next_follow_up_date);
                    ''')
                    db.execute('PRAGMA user_version=7')
                if version < 8:
                    # Workflow-only metadata: no ERP data or physical stock is
                    # changed by this migration. Preserve every existing task,
                    # follow-up, session, and print audit.
                    for statement in (
                        '''CREATE TABLE IF NOT EXISTS packing_product_owners(
                            group_key TEXT PRIMARY KEY,role TEXT NOT NULL,
                            assignee_id TEXT NOT NULL,updated_at TEXT NOT NULL)''',
                        '''CREATE TABLE IF NOT EXISTS daily_work_selections(
                            id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,role TEXT NOT NULL,
                            data_version TEXT NOT NULL,task_version INTEGER NOT NULL,
                            payload TEXT NOT NULL,created_at TEXT NOT NULL)''',
                        '''CREATE INDEX IF NOT EXISTS daily_selection_actor
                            ON daily_work_selections(actor_id,created_at)''',
                        '''CREATE TABLE IF NOT EXISTS daily_work_batches(
                            idempotency_key TEXT NOT NULL,actor_id TEXT NOT NULL,
                            request_hash TEXT NOT NULL,result TEXT NOT NULL,
                            created_at TEXT NOT NULL,PRIMARY KEY(actor_id,idempotency_key))''',
                        '''CREATE TABLE IF NOT EXISTS task_board_followups(
                            object_id TEXT PRIMARY KEY,result TEXT NOT NULL DEFAULT '',
                            waiting_for TEXT NOT NULL DEFAULT '',
                            promised_date TEXT NOT NULL DEFAULT '',
                            note TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL)''',
                    ):
                        db.execute(statement)
                    db.execute('PRAGMA user_version=8')
                if version < 9:
                    # Customer ownership and group lineage are local workflow
                    # facts. Existing follow-ups and their events remain intact.
                    columns = {row['name'] for row in db.execute(
                        'PRAGMA table_info(customer_order_followups)')}
                    if 'customer_name' not in columns:
                        db.execute("ALTER TABLE customer_order_followups ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")
                    for statement in (
                        '''CREATE TABLE IF NOT EXISTS sales_customer_owners(
                            profile_id TEXT NOT NULL, customer_name TEXT NOT NULL,
                            owner_id TEXT NOT NULL DEFAULT '', deputy_id TEXT NOT NULL DEFAULT '',
                            deputy_until TEXT NOT NULL DEFAULT '',
                            status TEXT NOT NULL DEFAULT 'unassigned',
                            source TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY(profile_id,customer_name))''',
                        '''CREATE TABLE IF NOT EXISTS customer_order_group_aliases(
                            legacy_group_id TEXT NOT NULL, group_id TEXT NOT NULL,
                            profile_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
                            customer_order_no TEXT NOT NULL, customer_name TEXT NOT NULL,
                            review_required INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY(legacy_group_id,group_id))''',
                        '''CREATE INDEX IF NOT EXISTS customer_group_alias_order
                            ON customer_order_group_aliases(profile_id,warehouse_id,customer_order_no)''',
                        '''CREATE TABLE IF NOT EXISTS sales_customer_owner_events(
                            id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                            customer_name TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                            actor_id TEXT NOT NULL, previous_owner_id TEXT NOT NULL,
                            owner_id TEXT NOT NULL, deputy_id TEXT NOT NULL,
                            deputy_until TEXT NOT NULL, reason TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            UNIQUE(profile_id,customer_name,idempotency_key))''',
                    ):
                        db.execute(statement)
                    db.execute('PRAGMA user_version=9')
                if version < 10:
                    # Store owns the consistent backup before print schema changes.
                    # New columns deliberately stay empty for historical actors;
                    # current task assignment is not evidence of who printed.
                    for statement in (
                        '''CREATE TABLE IF NOT EXISTS print_job_previews(
                            id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,
                            device_id TEXT NOT NULL,payload_json TEXT NOT NULL,
                            created_at TEXT NOT NULL)''',
                        '''CREATE INDEX IF NOT EXISTS print_preview_actor
                            ON print_job_previews(actor_id,created_at)''',
                        '''CREATE TABLE IF NOT EXISTS order_print_reset_requests(
                            actor_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                            request_fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY(actor_id,idempotency_key))''',
                        '''CREATE TABLE IF NOT EXISTS order_print_audit(
                            id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,order_no TEXT NOT NULL,
                            from_state TEXT NOT NULL DEFAULT '',to_state TEXT NOT NULL,
                            device_id TEXT NOT NULL DEFAULT '',job_id TEXT NOT NULL DEFAULT '',
                            reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)''',
                    ):
                        db.execute(statement)
                    columns = {row['name'] for row in db.execute('PRAGMA table_info(order_print_audit)')}
                    for name in ('actor_id', 'actor_name'):
                        if name not in columns:
                            db.execute(f"ALTER TABLE order_print_audit ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
                    db.execute('PRAGMA user_version=10')
                if version < 11:
                    db.execute('''CREATE TABLE IF NOT EXISTS print_state_corrections(
                        id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,payload_json TEXT NOT NULL,
                        preview_hash TEXT NOT NULL,idempotency_key TEXT UNIQUE,
                        result_json TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)''')
                    db.execute('PRAGMA user_version=11')

    @contextmanager
    def connect(self, write=False):
        reader = self._reader.get()
        if reader is not None and not write:
            yield reader
            return
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def snapshots(self):
        return snapshot_directory(self.snapshot_root)

    @contextmanager
    def read_snapshot(self):
        """One API response observes one SQLite generation, even across query modules."""
        if self._reader.get() is not None:
            yield
            return
        with self.connect() as db:
            db.execute("BEGIN")
            token = self._reader.set(db)
            try:
                yield
            finally:
                self._reader.reset(token)

    def source_list(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM sources ORDER BY kind")]

    def rows(self, kind):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE kind=?", (kind,))]

    def counts(self):
        with self.connect() as db:
            result = {kind: db.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()[0] for kind in ("orders", "inventory", "purchases")}
            tasks = [json.loads(row[0]) for row in db.execute("SELECT payload FROM tasks")]
            result.update(tasks=len(tasks), pending_tasks=sum(not task.get("completed", False) for task in tasks), completed_tasks=sum(bool(task.get("completed")) for task in tasks))
            return result
