"""Pinned independent history loader; all databases are synthetic and local."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.sales_history_matching import ensure_matching_schema, search_candidates
from core.sales_history_reference import SalesHistoryReference


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_source(db, profile="P", source_hash="a" * 64, version=1, active=1, metadata=None):
    if metadata is None:
        metadata = json.dumps({"source_sha256": source_hash, "coverage_start": "2026-07-01",
                               "coverage_end": "2026-07-01", "complete_current_history": False})
    db.execute("INSERT INTO sales_matching_sources VALUES (?,?,?,?,?,?,?,?,?,?)",
               (profile, source_hash, "synthetic.xls", 100, version, "2026-10-07T00:00:00Z",
                "2026-07-01", "2026-07-01", metadata, active))


def make_index(tmp_path, *, transform=None):
    # macOS /var is a symlink; the approved config must use its canonical path.
    path = tmp_path.resolve() / "any-fixed-config-name.sqlite3"
    with sqlite3.connect(path) as db:
        ensure_matching_schema(db)
        add_source(db)
        if transform:
            transform(db)
    return path, digest(path)


def test_absent_config_never_opens_a_reference(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unconfigured reference attempted to connect")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    reference = SalesHistoryReference()
    assert not reference.enabled and reference.path is None
    with reference.connect() as db:
        assert db is None
    with SalesHistoryReference("", "").connect() as db:
        assert db is None


@pytest.mark.parametrize("path,expected", [("/fixed.sqlite3", None), (None, "a" * 64),
                                         ("", "a" * 64), ("/fixed.sqlite3", "")])
def test_partial_config_is_rejected(path, expected):
    with pytest.raises(ValueError, match="config_incomplete"):
        SalesHistoryReference(path, expected)


def test_valid_index_is_immutable_query_only_and_each_connection_is_closed(tmp_path):
    path, expected = make_index(tmp_path)
    before = path.stat()
    reference = SalesHistoryReference(path, expected)
    assert reference.enabled and reference.path == path
    with reference.connect() as first:
        assert isinstance(first.execute("SELECT * FROM sales_matching_sources").fetchone(), sqlite3.Row)
        assert first.execute("PRAGMA query_only").fetchone()[0] == 1
        assert first.execute("SELECT count(*) FROM sales_matching_history").fetchone()[0] == 0
        for statement in ("UPDATE sales_matching_sources SET active=0", "CREATE TEMP TABLE secrets(v)",
                          "PRAGMA query_only=OFF", "ATTACH ':memory:' AS other"):
            with pytest.raises(sqlite3.DatabaseError):
                first.execute(statement)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first.execute("SELECT 1")
    with reference.connect() as second:
        assert second is not first and second.execute("SELECT count(*) FROM sales_matching_sources").fetchone()[0] == 1
    after = path.stat()
    assert digest(path) == expected
    assert (before.st_mtime_ns, before.st_ctime_ns, before.st_size) == (after.st_mtime_ns, after.st_ctime_ns, after.st_size)
    assert set(path.parent.iterdir()) == {path}


def test_connections_are_thread_local_instead_of_a_shared_flask_handle(tmp_path):
    path, expected = make_index(tmp_path)
    reference = SalesHistoryReference(path, expected)
    def read(index):
        with reference.connect() as db:
            return db.execute("SELECT profile_id FROM sales_matching_sources").fetchone()[0]
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(read, range(12))) == ["P"] * 12


@pytest.mark.parametrize("expected", ["", "abcd", "G" * 64, "A" * 64, " a" * 32])
def test_invalid_digest_is_rejected(tmp_path, expected):
    path, _ = make_index(tmp_path)
    with pytest.raises(ValueError, match="config_incomplete|hash_invalid"):
        SalesHistoryReference(path, expected)


def test_stale_or_wrong_sha_is_rejected(tmp_path):
    path, _ = make_index(tmp_path)
    with pytest.raises(ValueError, match="hash_mismatch"):
        SalesHistoryReference(path, "f" * 64)


def test_non_sqlite_or_corrupt_database_is_rejected_even_with_matching_hash(tmp_path):
    path = tmp_path.resolve() / "corrupt.sqlite3"
    path.write_bytes(b"not an SQLite file; do not attempt to migrate or fix")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="sqlite_invalid|integrity_failed"):
        SalesHistoryReference(path, digest(path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["records", "historical_orders", "credentials", "sales_orders", "sales_matching_secret"])
def test_raw_nas_or_business_tables_are_rejected(tmp_path, name):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute(f"CREATE TABLE {name}(value TEXT)"))
    with pytest.raises(ValueError, match="tables_invalid"):
        SalesHistoryReference(path, expected)


@pytest.mark.parametrize("statement", ["CREATE VIEW hidden AS SELECT * FROM sales_matching_sources",
    "CREATE TRIGGER silent_write AFTER INSERT ON sales_matching_history BEGIN DELETE FROM sales_matching_sources; END"])
def test_extra_views_and_triggers_are_rejected(tmp_path, statement):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute(statement))
    with pytest.raises(ValueError, match="schema_objects_invalid"):
        SalesHistoryReference(path, expected)


def test_missing_or_changed_schema_columns_are_rejected(tmp_path):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute("ALTER TABLE sales_matching_history ADD COLUMN unexpected TEXT"))
    with pytest.raises(ValueError, match="schema_invalid"):
        SalesHistoryReference(path, expected)


@pytest.mark.parametrize("version", [0, 2])
def test_source_schema_version_must_be_current(tmp_path, version):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute("UPDATE sales_matching_sources SET schema_version=?", (version,)))
    with pytest.raises(ValueError, match="schema_version_invalid"):
        SalesHistoryReference(path, expected)


def test_inactive_sources_with_old_schema_are_not_silently_accepted(tmp_path):
    path, expected = make_index(tmp_path, transform=lambda db: add_source(db, source_hash="b" * 64, version=0, active=0))
    with pytest.raises(ValueError, match="schema_version_invalid"):
        SalesHistoryReference(path, expected)


def test_unknown_global_version_is_rejected(tmp_path):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute("PRAGMA user_version=999"))
    with pytest.raises(ValueError, match="schema_version_invalid"):
        SalesHistoryReference(path, expected)


@pytest.mark.parametrize("active_count", [0, 2])
def test_each_profile_must_have_exactly_one_active_source(tmp_path, active_count):
    def change(db):
        if active_count == 0:
            db.execute("UPDATE sales_matching_sources SET active=0")
        else:
            add_source(db, source_hash="b" * 64)
    path, expected = make_index(tmp_path, transform=change)
    with pytest.raises(ValueError, match="active_source_not_unique"):
        SalesHistoryReference(path, expected)


def test_multiple_profiles_with_one_active_source_each_are_allowed(tmp_path):
    path, expected = make_index(tmp_path, transform=lambda db: add_source(db, profile="OTHER"))
    with SalesHistoryReference(path, expected).connect() as db:
        assert db.execute("SELECT count(*) FROM sales_matching_sources WHERE active=1").fetchone()[0] == 2


def test_empty_prepared_index_is_not_ready_for_configuration(tmp_path):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute("DELETE FROM sales_matching_sources"))
    with pytest.raises(ValueError, match="source_missing"):
        SalesHistoryReference(path, expected)


def test_invalid_metadata_is_rejected_before_search(tmp_path):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute("UPDATE sales_matching_sources SET metadata='broken-json'"))
    with pytest.raises(ValueError, match="source_metadata_invalid"):
        SalesHistoryReference(path, expected)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_nonempty_journal_sidecar_is_rejected(tmp_path, suffix):
    path, expected = make_index(tmp_path)
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"not yet settled")
    with pytest.raises(ValueError, match="unsettled_journal"):
        SalesHistoryReference(path, expected)
    assert sidecar.read_bytes() == b"not yet settled"


def test_real_uncheckpointed_wal_is_rejected_without_reading_or_checkpointing_it(tmp_path):
    path, _ = make_index(tmp_path)
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        add_source(writer, source_hash="b" * 64, active=0)
        writer.commit()
        sidecar = Path(str(path) + "-wal")
        before = sidecar.read_bytes()
        assert before
        with pytest.raises(ValueError, match="unsettled_journal"):
            SalesHistoryReference(path, digest(path))
        assert sidecar.read_bytes() == before
    finally:
        writer.close()


def test_symlink_leaf_and_parent_are_rejected(tmp_path):
    path, expected = make_index(tmp_path)
    leaf = tmp_path.resolve() / "leaf-link.sqlite3"
    leaf.symlink_to(path)
    with pytest.raises(ValueError, match="symlink_rejected"):
        SalesHistoryReference(leaf, expected)
    parent = tmp_path.resolve() / "parent-link"
    parent.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink_rejected"):
        SalesHistoryReference(parent / path.name, expected)


def test_missing_file_directory_relative_path_and_dotdot_escape_are_rejected(tmp_path):
    path, expected = make_index(tmp_path)
    cases = [(path.parent / "missing.sqlite3", "file_unavailable"), (path.parent, "regular_file_required"),
             (Path(path.name), "must_be_absolute"), (path.parent / "nested" / ".." / path.name, "must_be_absolute")]
    for candidate, reason in cases:
        with pytest.raises(ValueError, match=reason):
            SalesHistoryReference(candidate, expected)


def test_same_content_file_replacement_is_blocked_by_identity(tmp_path):
    path, expected = make_index(tmp_path)
    reference = SalesHistoryReference(path, expected)
    replacement = path.parent / "replacement.sqlite3"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    assert digest(path) == expected
    with pytest.raises(ValueError, match="file_changed"):
        with reference.connect():
            raise AssertionError("Replaced file was opened")


def test_inplace_change_with_original_size_and_mtime_is_blocked(tmp_path):
    path, expected = make_index(tmp_path)
    reference = SalesHistoryReference(path, expected)
    before = path.stat()
    with path.open("r+b") as handle:
        handle.seek(60)  # user_version, a fixed size header field
        handle.write(b"\x00\x00\x00\x01")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size and path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(ValueError, match="file_changed"):
        with reference.connect():
            pass


def test_file_replacement_during_read_prevents_successful_context_exit(tmp_path):
    path, expected = make_index(tmp_path)
    reference = SalesHistoryReference(path, expected)
    with pytest.raises(ValueError, match="file_changed"):
        with reference.connect() as db:
            assert db.execute("SELECT count(*) FROM sales_matching_sources").fetchone()[0] == 1
            replacement = path.parent / "replacement.sqlite3"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)


def test_runtime_symlink_or_new_journal_is_blocked_before_connection(tmp_path):
    path, expected = make_index(tmp_path)
    reference = SalesHistoryReference(path, expected)
    journal = Path(str(path) + "-journal")
    journal.write_bytes(b"new journal")
    with pytest.raises(ValueError, match="unsettled_journal"):
        with reference.connect():
            pass
    journal.unlink()
    moved = path.parent / "moved.sqlite3"
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(ValueError, match="symlink_rejected"):
        with reference.connect():
            pass


def test_genuine_sqlite_statistics_are_allowed(tmp_path):
    path, expected = make_index(tmp_path, transform=lambda db: db.execute("ANALYZE"))
    with SalesHistoryReference(path, expected).connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()


def test_reference_connection_can_query_history_but_never_supply_current_catalog(tmp_path):
    def history(db):
        attrs = {"raw": {"name": "六角头螺栓", "spec": "M6*30", "grade": "8.8", "surface": "发黑", "material": ""},
                 "name": "六角头螺栓", "spec": "m6x30", "grade": "8.8", "surface": "发黑", "material": "", "standards": []}
        db.execute("INSERT INTO sales_matching_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("P", "a" * 64, "row1", "客户甲", "C1", "PART-1", "LS1", "2026-07-01",
                    "synthetic.xls", "1", "synthetic original summary", json.dumps(attrs),
                    attrs["name"], attrs["spec"], attrs["grade"], attrs["surface"], 1, "[]"))
    path, expected = make_index(tmp_path, transform=history)
    reference = SalesHistoryReference(path, expected)
    current = sqlite3.connect(":memory:")
    current.execute("CREATE TABLE records(kind TEXT,id TEXT,payload TEXT,sku TEXT)")
    product = {"profile_id": "P", "warehouse_id": "W", "warehouse_name": "江苏优螺", "product_id": "current-product",
               "sku_id": "current-sku", "base_unit": "1", "product_name": "六角头螺栓", "spec": "M6*30", "grade": "8.8", "surface": "发黑", "unit_name": "支"}
    current.execute("INSERT INTO records VALUES (?,?,?,?)", ("inventory", "current-row", json.dumps(product), "LS1"))
    with reference.connect() as history_db:
        result = search_candidates(current, "P", "客户甲", {"customer_part_no": "PART-1"}, history_db=history_db)
        assert result["candidates"][0]["current_identity"]["product_id"] == "current-product"
        assert result["candidates"][0]["available"]
        current.execute("DELETE FROM records")
        result = search_candidates(current, "P", "客户甲", {"customer_part_no": "PART-1"}, history_db=history_db)
        assert not result["candidates"][0]["available"]
    current.close()
