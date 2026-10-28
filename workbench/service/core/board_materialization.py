"""Bounded process-local views keyed by the *reader's* committed SQLite facts.

Public data/task versions deliberately do not change for every workflow fact.
A private trigger counter covers those changes without altering API versions or
print-job ownership. Reading it inside the response's read transaction prevents
a concurrent writer from mixing a cached view with another database snapshot.
"""
from collections import OrderedDict
from threading import RLock
import uuid


TABLES = frozenset({
    'records', 'settings', 'staff_users', 'logistics_calculations',
    'daily_work_settings', 'daily_work_tasks', 'daily_work_events',
    'sales_customer_owners', 'customer_order_group_aliases',
    'customer_order_followups', 'customer_order_followup_events',
    'customer_order_work_actions', 'customer_order_board_snapshots',
    'order_lifecycle_state', 'order_print_states', 'order_print_audit', 'print_jobs',
    'task_board_followups',
})


class BoardMaterialization:
    def __init__(self, store, limit=32):
        self.store, self.limit = store, limit
        self._lock = RLock()
        self._entries = OrderedDict()
        self._build_locks = {}
        self._installed = None
        self.prepare()

    def _file_identity(self):
        value = self.store.path.stat()
        return value.st_dev, value.st_ino

    def prepare(self):
        # A caller may already be holding a response snapshot. Never open a
        # separate writer to install schema while that snapshot is in use.
        if self.store._reader.get() is not None:
            return
        with self._lock, self.store.connect() as db:
            identity = self._file_identity()
            schema = db.execute('PRAGMA schema_version').fetchone()[0]
            if self._installed == (identity, schema):
                return
        with self._lock, self.store.connect(write=True) as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db.execute('''CREATE TABLE IF NOT EXISTS task_board_materialization_generation(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                incarnation TEXT NOT NULL,generation INTEGER NOT NULL)''')
            db.execute('''INSERT OR IGNORE INTO task_board_materialization_generation
                VALUES (1,?,0)''', (uuid.uuid4().hex,))
            for table in sorted(TABLES & existing):
                for event in ('INSERT', 'UPDATE', 'DELETE'):
                    condition = ''
                    if table == 'records':
                        facts = []
                        if event != 'DELETE': facts.append("NEW.kind IN ('orders','inventory')")
                        if event != 'INSERT': facts.append("OLD.kind IN ('orders','inventory')")
                        condition = ' WHEN ' + ' OR '.join(facts)
                    elif table == 'settings':
                        # Worker heartbeats, UI preferences and queue progress
                        # are not packaging facts. They used to evict every page.
                        watched = "('data_version','data_source_updated_at','published_profile','restoration_hold')"
                        facts=[]
                        if event != 'DELETE':facts.append(f'NEW.key IN {watched}')
                        if event != 'INSERT':facts.append(f'OLD.key IN {watched}')
                        condition = ' WHEN ' + ' OR '.join(facts)
                        db.execute(f'DROP TRIGGER IF EXISTS board_generation_{table}_{event.lower()}')
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS board_generation_{table}_{event.lower()}
                        AFTER {event} ON {table}{condition} BEGIN
                        UPDATE task_board_materialization_generation SET generation=generation+1
                        WHERE singleton=1; END''')
            # Also invalidates materializations if a formerly absent input
            # table was populated before its triggers could be installed.
            db.execute('UPDATE task_board_materialization_generation SET generation=generation+1 WHERE singleton=1')
            self._installed = (identity, db.execute('PRAGMA schema_version').fetchone()[0])
            self._entries.clear()

    def generation(self, db):
        if self.store._reader.get() is not db:
            # Write operations always reconstruct their exact scope in the
            # write transaction; they never consume a cached permission view.
            return None
        schema = db.execute('PRAGMA schema_version').fetchone()[0]
        identity = self._file_identity()
        if self._installed != (identity, schema):
            return None
        row = db.execute('SELECT incarnation,generation FROM task_board_materialization_generation WHERE singleton=1').fetchone()
        return (*identity, schema, row[0], row[1]) if row else None

    def get(self, generation, key, build):
        if generation is None:
            return build()
        identity = (generation, key)
        with self._lock:
            if identity in self._entries:
                self._entries.move_to_end(identity)
                return self._entries[identity]
            lock, users = self._build_locks.get(identity, (RLock(), 0))
            self._build_locks[identity] = (lock, users + 1)
        try:
            with lock:
                with self._lock:
                    if identity in self._entries:
                        self._entries.move_to_end(identity)
                        return self._entries[identity]
                value = build()
                with self._lock:
                    self._publish(identity, value)
                return value
        finally:
            with self._lock:
                _, users = self._build_locks[identity]
                if users == 1: del self._build_locks[identity]
                else: self._build_locks[identity] = (lock, users - 1)

    def _publish(self, identity, value):
        # Publish only complete successful views. Exceptions leave the last
        # valid entries intact and cannot publish a partial generation.
        self._entries[identity] = value
        # An inventory model is much larger than a page. Keep at most two
        # reader generations even when a print/action changes facts often.
        generations=list(dict.fromkeys(item[0] for item in reversed(self._entries)))
        retained=set(generations[:2])
        for item in list(self._entries):
            if item[0] not in retained:
                del self._entries[item]
        while len(self._entries) > self.limit:
            self._entries.popitem(last=False)
