"""Low-frequency source stat checks; refresh only the independent copy/database."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from .snapshots import SOURCES, capture
from .store import timestamp
from .import_safety import ImportBusy


class SourceMonitor:
    def __init__(self, store, importer, interval=60):
        self.store, self.importer, self.interval = store, importer, max(30, int(interval))
        self.stop = threading.Event()
        self.thread = None
        self.state = {'running': False, 'interval_seconds': self.interval, 'checked_at': None, 'last_success_at': None, 'last_error': '', 'refreshing': False}

    def changed(self):
        path = self.store.snapshots / 'manifest.json'
        if not path.exists():
            return True
        files = json.loads(path.read_text(encoding='utf-8-sig')).get('files', {})
        imported = {item['kind']: item for item in self.store.source_list()}
        for kind, (source, _) in SOURCES.items():
            stat = Path(source).stat()
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            # A new snapshot can exist after parsing/publication fails. Compare
            # the live database metadata too, so it retries until actually imported.
            if imported.get(kind, {}).get('status') != 'ready' or imported.get(kind, {}).get('source_modified_at') != modified or imported.get(kind, {}).get('sha256') != files.get(kind, {}).get('sha256') or files.get(kind, {}).get('size') != stat.st_size:
                return True
        return False

    def check_once(self):
        self.state['checked_at'] = timestamp()
        try:
            if self.changed():
                self.state['refreshing'] = True
                self.importer.run(refresh=lambda: capture(self.store.runtime))
                self.state['last_success_at'] = timestamp()
            self.state['last_error'] = ''
        except ImportBusy:
            pass  # an explicit refresh already owns this same runtime lock
        except Exception as exc:
            self.state['last_error'] = str(exc)
        finally:
            self.state['refreshing'] = False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.state['running'] = True
        def loop():
            while not self.stop.wait(self.interval):
                self.check_once()
            self.state['running'] = False
        self.thread = threading.Thread(target=loop, name='android-source-watch', daemon=True)
        self.thread.start()

    def status(self):
        return dict(self.state)
