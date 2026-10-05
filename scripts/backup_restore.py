"""Backup/verify/restore either edition's database using SQLite's online backup API."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prompt_core.backup import backup_database, restore_database, verify_backup
from prompt_core.instance_lock import acquire_instance_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--database", required=True)
    backup.add_argument("--output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--manifest", required=True)
    restore.add_argument("--database", required=True)
    restore.add_argument("--service-stopped", action="store_true")
    restore.add_argument("--edition", choices=("main", "lite"), default="main")
    args = parser.parse_args()
    if args.command == "backup":
        print(backup_database(args.database, args.output))
    elif args.command == "verify":
        _path, manifest = verify_backup(args.manifest)
        print(json.dumps({"verified": True, "counts": manifest["counts"]}, ensure_ascii=False))
    else:
        database = Path(args.database).resolve()
        lock_path = database.parent / "server.lock" if args.edition == "lite" else Path(str(database) + ".service.lock")
        try:
            lock = acquire_instance_lock(lock_path)
        except OSError:
            parser.exit(1, "服务正在运行，不能恢复此数据库；请先停止对应服务。\n")
        try:
            print(json.dumps(restore_database(args.manifest, database,
                                             service_stopped=args.service_stopped), ensure_ascii=False))
        finally:
            lock.close()


if __name__ == "__main__":
    main()
