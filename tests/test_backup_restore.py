import json
import sqlite3

import pytest

from prompt_core.backup import backup_database, restore_database, verify_backup


def test_backup_includes_committed_wal_and_restores_without_touching_source(tmp_path):
    db_path = tmp_path / "live.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO records VALUES (1, 'original')")
        conn.commit()
        manifest = backup_database(db_path, tmp_path / "backup")
        _path, info = verify_backup(manifest)
        assert info["counts"]["records"] == 1
        conn.execute("INSERT INTO records VALUES (2, 'later')")
        conn.commit()
        restored = tmp_path / "restored.db"
        restore_database(manifest, restored, service_stopped=True)
        with sqlite3.connect(restored) as result:
            assert result.execute("SELECT value FROM records").fetchall() == [("original",)]
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2
        with pytest.raises(ValueError, match="WAL"):
            restore_database(manifest, db_path, service_stopped=True)
    finally:
        conn.close()


def test_restore_rejects_corruption_and_preserves_existing_target(tmp_path):
    original = tmp_path / "source.db"
    with sqlite3.connect(original) as conn:
        conn.execute("CREATE TABLE old_data (value TEXT)")
        conn.execute("INSERT INTO old_data VALUES ('preserve')")
    manifest = backup_database(original, tmp_path / "backup")
    with pytest.raises(ValueError, match="Stop"):
        restore_database(manifest, tmp_path / "out.db")
    path = manifest.parent / "snapshot.db"
    with path.open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError, match="checksum"):
        restore_database(manifest, original, service_stopped=True)
    with sqlite3.connect(original) as conn:
        assert conn.execute("SELECT value FROM old_data").fetchone()[0] == "preserve"


def test_existing_target_is_backed_up_before_restore(tmp_path):
    original = tmp_path / "original.db"
    target = tmp_path / "target.db"
    for path, value in ((original, "new"), (target, "old")):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE records (value TEXT)")
            conn.execute("INSERT INTO records VALUES (?)", (value,))
    manifest = backup_database(original, tmp_path / "backup")
    result = restore_database(manifest, target, service_stopped=True)
    old_path, _info = verify_backup(result["previous_manifest"])
    with sqlite3.connect(old_path) as conn:
        assert conn.execute("SELECT value FROM records").fetchone()[0] == "old"
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT value FROM records").fetchone()[0] == "new"
