"""Incremental, checksummed SQLite migrations, applied atomically."""
import hashlib
import json
import re


def add_column(table, column, declaration):
    return ("column", table, column, declaration)


def sql(statement):
    return ("sql", statement)


def apply_migrations(conn, migrations):
    if conn.in_transaction:
        raise RuntimeError("Migrations require a connection without a pending transaction")
    applied = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                     "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        for version, operations in migrations:
            checksum = hashlib.sha256(json.dumps(operations, ensure_ascii=False,
                                                 separators=(",", ":")).encode()).hexdigest()
            old = conn.execute("SELECT checksum FROM schema_migrations WHERE version=?", (version,)).fetchone()
            if old:
                if old[0] != checksum:
                    raise RuntimeError("Migration checksum changed: " + version)
                continue
            for operation in operations:
                if operation[0] == "sql":
                    conn.execute(operation[1])
                elif operation[0] == "column":
                    _, table, column, declaration = operation
                    if not all(re.fullmatch(r"[a-z_][a-z0-9_]*", name) for name in (table, column)):
                        raise ValueError("Invalid migration identifier")
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
                    if column not in columns:
                        conn.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + declaration)
                else:
                    raise ValueError("Unknown migration operation")
            conn.execute("INSERT INTO schema_migrations(version,checksum) VALUES(?,?)", (version, checksum))
            applied.append(version)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return applied
