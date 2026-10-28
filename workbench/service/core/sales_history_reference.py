"""Load only a pinned, independent sales-history index, without importing it.

The service configuration owns the path and digest. Each request gets a new
immutable, read-only connection; current inventory remains in the business DB.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .sales_history_matching import SCHEMA_VERSION


_TABLES = frozenset(("sales_matching_sources", "sales_matching_history"))
_INTERNAL_TABLES = frozenset(("sqlite_sequence", "sqlite_stat1", "sqlite_stat2", "sqlite_stat3", "sqlite_stat4"))
_COLUMNS = {
    "sales_matching_sources": (
        ("profile_id", "TEXT", 1), ("source_sha256", "TEXT", 2),
        ("source_name", "TEXT", 0), ("source_bytes", "INTEGER", 0),
        ("schema_version", "INTEGER", 0), ("imported_at", "TEXT", 0),
        ("coverage_start", "TEXT", 0), ("coverage_end", "TEXT", 0),
        ("metadata", "TEXT", 0), ("active", "INTEGER", 0),
    ),
    "sales_matching_history": (
        ("profile_id", "TEXT", 1), ("source_sha256", "TEXT", 2),
        ("source_row_id", "TEXT", 3), ("customer_name", "TEXT", 0),
        ("customer_code", "TEXT", 0), ("customer_part_no", "TEXT", 0),
        ("erp_code", "TEXT", 0), ("order_date", "TEXT", 0),
        ("source_file", "TEXT", 0), ("source_excel_row", "TEXT", 0),
        ("raw_summary", "TEXT", 0), ("attributes", "TEXT", 0),
        ("name_key", "TEXT", 0), ("spec_key", "TEXT", 0),
        ("grade_key", "TEXT", 0), ("surface_key", "TEXT", 0),
        ("eligible", "INTEGER", 0), ("review_reasons", "TEXT", 0),
    ),
}
_READ_PRAGMAS = frozenset((
    "table_info", "table_xinfo", "index_info", "index_xinfo", "index_list",
    "integrity_check", "quick_check", "database_list", "schema_version", "user_version",
    "query_only", "journal_mode",
))


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _read_authorizer(action, first, second, database, trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        allowed = first in _TABLES | _INTERNAL_TABLES or first in ("sqlite_master", "sqlite_schema")
        # SQLite reports database=None for the implicit table read of count(*).
        # No attached/temp databases can be added through this connection.
        return sqlite3.SQLITE_OK if database in ("main", None) and allowed else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if str(second).lower() == "load_extension" else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        # Introspection pragmas take an object name; settings must remain read-only.
        introspection = first in ("table_info", "table_xinfo", "index_info", "index_xinfo", "index_list")
        return sqlite3.SQLITE_OK if first in _READ_PRAGMAS and (second is None or introspection) else sqlite3.SQLITE_DENY
    # No ATTACH, writes, temporary tables, schema edits or transactions with writes.
    return sqlite3.SQLITE_DENY


class SalesHistoryReference:
    """A fixed service-configured reference; no path is accepted from a request.

    Both configuration values absent disables the reference. Partial or invalid
    configuration raises ValueError at startup. connect() yields None when
    disabled and a fresh sqlite3.Row connection otherwise.
    """

    def __init__(self, path=None, expected_sha256=None):
        self.enabled = False
        self.path = None
        self.expected_sha256 = None
        self._identity = None
        path_missing = path is None or path == ""
        digest_missing = expected_sha256 is None or expected_sha256 == ""
        if path_missing and digest_missing:
            return
        if path_missing or digest_missing:
            raise ValueError("history_reference_config_incomplete")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("history_reference_hash_invalid")
        try:
            requested = Path(path)
        except (TypeError, ValueError):
            raise ValueError("history_reference_path_invalid") from None
        if not requested.is_absolute() or ".." in requested.parts:
            raise ValueError("history_reference_path_must_be_absolute")
        self.path = requested
        self.expected_sha256 = expected_sha256
        self._identity = _fingerprint(self._path_stat())
        self._assert_no_journal()
        if self._file_sha256() != expected_sha256:
            raise ValueError("history_reference_hash_mismatch")
        self._assert_unchanged()
        try:
            connection = self._open()
            try:
                self._validate_schema(connection)
            finally:
                connection.close()
        except sqlite3.Error:
            raise ValueError("history_reference_sqlite_invalid") from None
        self._assert_unchanged()
        self.enabled = True

    def _path_stat(self):
        try:
            for item in (self.path, *self.path.parents):
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("history_reference_symlink_rejected")
                if item == self.path:
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("history_reference_regular_file_required")
                    file_info = info
                elif not stat.S_ISDIR(info.st_mode):
                    raise ValueError("history_reference_parent_invalid")
            return file_info
        except OSError:
            raise ValueError("history_reference_file_unavailable") from None

    def _assert_no_journal(self):
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                info = Path(str(self.path) + suffix).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise ValueError("history_reference_journal_unavailable") from None
            if not stat.S_ISREG(info.st_mode) or info.st_size:
                raise ValueError("history_reference_unsettled_journal")

    def _assert_unchanged(self):
        if _fingerprint(self._path_stat()) != self._identity:
            raise ValueError("history_reference_file_changed")
        self._assert_no_journal()

    def _file_sha256(self):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            with os.fdopen(os.open(self.path, flags), "rb") as handle:
                if _fingerprint(os.fstat(handle.fileno())) != self._identity:
                    raise ValueError("history_reference_file_changed")
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                if _fingerprint(os.fstat(handle.fileno())) != self._identity:
                    raise ValueError("history_reference_file_changed")
                return digest.hexdigest()
        except OSError:
            raise ValueError("history_reference_file_unavailable") from None

    def _open(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            self._assert_unchanged()
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _validate_schema(connection):
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if len(integrity) != 1 or integrity[0][0] != "ok":
            raise ValueError("history_reference_integrity_failed")
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master").fetchall()
        tables = {row[1] for row in schema if row[0] == "table"}
        if not _TABLES.issubset(tables) or any(
                name not in _TABLES | _INTERNAL_TABLES for name in tables):
            raise ValueError("history_reference_tables_invalid")
        if any(row[0] not in ("table", "index") or
               (row[2] not in _TABLES | _INTERNAL_TABLES) or
               (row[0] == "table" and "VIRTUAL TABLE" in (row[3] or "").upper())
               for row in schema):
            raise ValueError("history_reference_schema_objects_invalid")
        for table, expected in _COLUMNS.items():
            actual = connection.execute(f"PRAGMA table_info({table})").fetchall()
            contract = tuple((row[1], row[2].upper(), row[5]) for row in actual)
            if contract != expected or any(row[3] != 1 for row in actual):
                raise ValueError("history_reference_schema_invalid")
        # The existing isolated importer versions source rows, not user_version.
        # Accept its zero marker and an explicit current marker; reject other versions.
        if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, SCHEMA_VERSION):
            raise ValueError("history_reference_schema_version_invalid")
        sources = connection.execute(
            "SELECT profile_id,source_sha256,schema_version,active,metadata FROM sales_matching_sources").fetchall()
        if not sources:
            raise ValueError("history_reference_source_missing")
        active = {}
        for profile, source_hash, version, is_active, metadata in sources:
            if not isinstance(profile, str) or not profile.strip() or not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
                raise ValueError("history_reference_source_invalid")
            if version != SCHEMA_VERSION or type(version) is not int:
                raise ValueError("history_reference_schema_version_invalid")
            if type(is_active) is not int or is_active not in (0, 1):
                raise ValueError("history_reference_source_invalid")
            active[profile] = active.get(profile, 0) + is_active
            try:
                if not isinstance(json.loads(metadata), dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("history_reference_source_metadata_invalid") from None
        if any(count != 1 for count in active.values()):
            raise ValueError("history_reference_active_source_not_unique")
        if connection.execute("""SELECT 1 FROM sales_matching_history h
            LEFT JOIN sales_matching_sources s ON s.profile_id=h.profile_id AND s.source_sha256=h.source_sha256
            WHERE s.profile_id IS NULL LIMIT 1""").fetchone():
            raise ValueError("history_reference_history_source_missing")

    @contextmanager
    def connect(self):
        if not self.enabled:
            yield None
            return
        self._assert_unchanged()
        try:
            connection = self._open()
        except sqlite3.Error:
            raise ValueError("history_reference_sqlite_invalid") from None
        connection.set_authorizer(_read_authorizer)
        try:
            yield connection
        finally:
            connection.close()
            self._assert_unchanged()
