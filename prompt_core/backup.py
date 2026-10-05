"""Verified SQLite snapshots. Restore requires the owning service to be stopped."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else \
            hashlib.sha256(stream.read()).hexdigest()


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def inspect_database(conn):
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if integrity != ["ok"] or conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Database integrity or foreign key check failed")
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        counts[table] = conn.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0]
    versions = conn.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall() \
        if "schema_migrations" in tables else []
    return {"counts": counts, "migrations": [list(row) for row in versions]}


def backup_database(database, directory):
    database, directory = Path(database).resolve(), Path(directory).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    directory.mkdir(parents=True, exist_ok=True)
    output, manifest_path = directory / "snapshot.db", directory / "manifest.json"
    if output.exists() or manifest_path.exists():
        raise FileExistsError("Backup destination is not empty")
    source, dest = readonly(database), sqlite3.connect(output)
    try:
        source.backup(dest)
        metadata = inspect_database(dest)
    finally:
        dest.close()
        source.close()
    manifest = {"format_version": 1, "file": output.name, "sha256": digest(output),
                "created_at": datetime.now(timezone.utc).isoformat(), **metadata}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def verify_backup(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filename = manifest.get("file", "")
    if manifest.get("format_version") != 1 or filename != "snapshot.db":
        raise ValueError("Unsupported backup manifest")
    output = manifest_path.parent / filename
    if output.resolve().parent != manifest_path.parent or digest(output) != manifest.get("sha256"):
        raise ValueError("Backup checksum mismatch")
    conn = readonly(output)
    try:
        metadata = inspect_database(conn)
    finally:
        conn.close()
    if any(manifest.get(key) != metadata[key] for key in metadata):
        raise ValueError("Backup metadata mismatch")
    return output, manifest


def restore_database(manifest_path, target, *, service_stopped=False):
    if service_stopped is not True:
        raise ValueError("Stop the owning service before restoring")
    source_path, manifest = verify_backup(manifest_path)
    target = Path(target).resolve()
    if target == source_path.resolve():
        raise ValueError("Restore target cannot be the backup itself")
    # Refuse potentially live WAL storage; do not delete sidecars as a shortcut.
    if any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise ValueError("Target has WAL sidecars; stop all database connections first")
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        previous = backup_database(target, target.parent / (target.stem + "-before-restore-" + stamp))
    source, dest = readonly(source_path), sqlite3.connect(target)
    try:
        source.backup(dest)
        if inspect_database(dest)["counts"] != manifest["counts"]:
            raise ValueError("Restored row counts mismatch")
    finally:
        dest.close()
        source.close()
    return {"target": str(target), "previous_manifest": str(previous) if previous else None}
