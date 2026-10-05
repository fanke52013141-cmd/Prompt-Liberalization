import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

from prompt_lib.db import DB
from prompt_lib.ledger import BudgetState, Ledger


def test_real_process_recovery_preserves_unknown_budget_and_blocks_second_instance(tmp_path):
    db_path = str(tmp_path / "service.db")
    db = DB(db_path)
    budget = {"mode": "token", "total_limit": 5000, "search_limit": 5000, "acceptance_limit": 0}
    for rid, state in (("interrupted", "running"), ("stopped", "stopping")):
        db.execute("INSERT INTO runs(id,project_id,snapshot_json,snapshot_hash,state,budget_state_json,created_at,updated_at) "
                   "VALUES(?,'p','{}','hash',?,?,'now','now')", (rid, state, json.dumps(budget)))
    Ledger(db).reserve("interrupted", "pending-call", "generation", "mock", "search", 400, BudgetState(budget))
    db._conn.close()
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "run_server.py"), "--db", db_path, "--port", str(port)]
    options = {"cwd": str(root), "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
               "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
               "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
    service = subprocess.Popen(command, **options)
    try:
        deadline = time.monotonic() + 15
        while True:
            if service.poll() is not None:
                raise AssertionError(service.communicate())
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/workflow-api/v1/runs/interrupted", timeout=1) as response:
                    recovered = json.load(response)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("Owned service did not start")
                time.sleep(0.05)
        assert recovered["state"] == "paused_interrupted"
        assert recovered["budget"]["reserved"]["search"] == 400
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/workflow-api/v1/runs/stopped", timeout=1) as response:
            assert json.load(response)["state"] == "cancelled"
        second = subprocess.run(command, timeout=10, **options)
        assert second.returncode == 1
        assert "已有服务运行" in second.stderr.decode("utf-8")
        from prompt_core.backup import backup_database
        manifest = backup_database(db_path, tmp_path / "snapshot")
        restore = subprocess.run([sys.executable, str(root / "scripts/backup_restore.py"), "restore",
                                  "--database", db_path, "--manifest", str(manifest), "--service-stopped"],
                                 timeout=10, **options)
        assert restore.returncode == 1
        assert "服务正在运行" in restore.stderr.decode("utf-8")
    finally:
        service.terminate()
        service.communicate(timeout=10)
    reopened = DB(db_path)
    try:
        assert reopened.one("SELECT status FROM ledger WHERE logical_id='pending-call'")["status"] == "sent_unknown"
        assert reopened.one("SELECT state FROM runs WHERE id='interrupted'")["state"] == "paused_interrupted"
    finally:
        reopened._conn.close()
