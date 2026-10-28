"""Validated SQLite cache for stable ERP reference data."""
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .store import dumps


class ReferenceCache:
    def __init__(self, store, max_age=timedelta(hours=24)):
        self.store = store
        self.max_age = max_age

    def get(self, kind, profile, now=None):
        entry = self.get_entry(kind, profile, now=now)
        if entry is None:
            return None
        now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        if now - entry['fetched_at'] > self.max_age:
            return None
        return entry['payload']

    def get_entry(self, kind, profile, now=None):
        """Return an integrity-checked entry even when its TTL has expired."""
        now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        with self.store.connect() as database:
            row = database.execute(
                "SELECT profile,payload,fetched_at,source_hash FROM erp_reference_cache WHERE kind=?",
                (kind,),
            ).fetchone()
        if row is None or str(row["profile"]) != str(profile):
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            payload = json.loads(row["payload"])
            encoded = dumps(payload)
            if fetched.tzinfo is None or fetched > now:
                return None
            if hashlib.sha256(encoded.encode()).hexdigest() != row["source_hash"]:
                return None
            return {'payload': payload, 'fetched_at': fetched}
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def put(self, kind, profile, payload, now=None):
        now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        encoded = dumps(payload)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.store.connect(write=True) as database:
            database.execute(
                "INSERT OR REPLACE INTO erp_reference_cache VALUES(?,?,?,?,?)",
                (kind, str(profile), encoded, now.isoformat(timespec="seconds"), digest),
            )
        return payload

    def invalidate(self, kind=None):
        with self.store.connect(write=True) as database:
            if kind is None:
                database.execute("DELETE FROM erp_reference_cache")
            else:
                database.execute("DELETE FROM erp_reference_cache WHERE kind=?", (kind,))
