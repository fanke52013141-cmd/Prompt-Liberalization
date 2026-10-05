"""Optional real Chromium validation against an owned temporary main service."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--node-modules", required=True)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    import uvicorn
    from fastapi.testclient import TestClient
    from prompt_lib.api import create_app
    from prompt_lib.db import DB, set_db
    from conftest import setup_project_with_data, start_run
    with tempfile.TemporaryDirectory(prefix="main-browser-budget-") as directory:
        db = DB(str(Path(directory) / "test.db"))
        set_db(db)
        app = create_app(str(ROOT / "web"))
        with TestClient(app) as client:
            from tests.test_human_acceptance import setup_exam
            human_data, human_run, _human_view = setup_exam(client)
            from tests.test_severity_audits import fixture_audit
            severity_tasks=[]
            for _ in range(2):
                j,body=fixture_audit(client)
                body['budget']['total_limit']=body['budget']['search_limit']=1
                response=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
                response.raise_for_status()
                severity_tasks.append(response.json() | {'build_gold_ids':body['build_gold_ids'],
                                                        'audit_gold_ids':body['audit_gold_ids']})
            from prompt_lib.ledger import BudgetState, Ledger
            j,body=fixture_audit(client)
            response=client.post(f"/workflow-api/v1/judges/{j['id']}/severity-audits",json=body)
            response.raise_for_status()
            unknown_task=response.json()
            ledger=Ledger(db)
            unknown_budget=BudgetState(unknown_task['budget'])
            attempt=ledger.reserve(unknown_task['id'],'browser-unknown','evaluation','mock-gen-1','search',30,
                                   unknown_budget,'browser-audit-fingerprint')
            ledger.mark_sent_unknown(attempt,unknown_budget)
            severity_tasks.append(unknown_task | {'unknown_attempt':attempt})
            data = setup_project_with_data(client)
            response = client.put("/workflow-api/v1/settings/prices", json={
                "mock-gen-1": {"in_per_1k": "0.001", "out_per_1k": "0.002", "currency": "CNY"}})
            response.raise_for_status()
            usage_run = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                                  dev_ids=data["item_ids"][:2], max_candidates=0,
                                  budget={"mode": "money", "total_limit": "1.5",
                                          "search_limit": "1.0", "acceptance_limit": "0.5"})
            if usage_run["state"] != "completed":
                raise RuntimeError("Usage reconciliation browser fixture failed")
            gold_outputs=db.query('SELECT id FROM outputs WHERE run_id=? ORDER BY rowid',(usage_run['id'],))
            db.execute('UPDATE outputs SET text=? WHERE id=?',('😀重复😀重复',gold_outputs[1]['id']))
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            from prompt_lib.engine import call_model
            from prompt_lib.providers import CallResult, ProviderError
            from prompt_lib.ledger import BudgetState, Ledger
            from prompt_lib import engine
            original_factory = engine._get_provider
            interrupted_result = {}
            def interrupted_provider(config):
                original = original_factory(config)
                def complete(role, model, messages, params, fingerprint):
                    result = original.complete(role, model, messages, params, fingerprint)
                    if role == 'generation' and not interrupted_result:
                        interrupted_result['result'] = result
                        raise ProviderError('NETWORK', 'offline browser run interrupted', retryable=False)
                    return result
                return SimpleNamespace(complete=complete)
            with patch('prompt_lib.engine._get_provider', side_effect=interrupted_provider):
                interrupted_run = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                                            dev_ids=data['item_ids'][:2], max_candidates=0,
                                            budget={'mode': 'money', 'total_limit': '1.5',
                                                    'search_limit': '1.0', 'acceptance_limit': '0.5'})
            if interrupted_run['state'] != 'paused_interrupted':
                raise RuntimeError('Browser interrupted run fixture did not pause')
            with patch("prompt_lib.engine._get_provider", return_value=SimpleNamespace(
                    complete=Mock(return_value=CallResult("saved browser output", "stop", {})))):
                call_model(db, "generation", usage_run["snapshot"]["models"]["generation"],
                           [{"role": "user", "content": "browser usage fixture"}], {}, usage_run["id"],
                           "browser-usage", "search", BudgetState(usage_run["budget"]), Ledger(db))
            with patch("prompt_lib.engine._get_provider", return_value=SimpleNamespace(
                    complete=Mock(side_effect=ProviderError("NETWORK", "lost browser response", retryable=False)))):
                try:
                    call_model(db, "generation", usage_run["snapshot"]["models"]["generation"],
                               [{"role": "user", "content": "browser response fixture"}], {}, usage_run["id"],
                               "browser-response", "search", BudgetState(usage_run["budget"]), Ledger(db))
                except ProviderError:
                    pass
                try:
                    call_model(db, "generation", usage_run["snapshot"]["models"]["generation"],
                               [{"role": "user", "content": "browser rejected fixture"}], {}, usage_run["id"],
                               "browser-not-accepted", "search", BudgetState(usage_run["budget"]), Ledger(db))
                except ProviderError:
                    pass
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Owned browser service failed to start")
                time.sleep(0.05)
            output = ROOT / ".verification-results" / "main-budget.png"
            output.parent.mkdir(exist_ok=True)
            subprocess.run([args.node, str(ROOT / "scripts" / "browser_budget.js"),
                            f"http://127.0.0.1:{port}", args.chrome, data["pid"], str(output), usage_run["id"],
                            interrupted_run['id'], json.dumps({
                                'text': interrupted_result['result'].text, 'finish': interrupted_result['result'].finish,
                                'usage': interrupted_result['result'].usage}), human_data['pid'], human_run['id'],json.dumps(severity_tasks),json.dumps([r['id'] for r in gold_outputs])],
                           env={**os.environ, "NODE_PATH": args.node_modules}, check=True, timeout=120)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            db._conn.close()
            if thread.is_alive():
                raise RuntimeError("Owned browser service did not stop")


if __name__ == "__main__":
    main()
