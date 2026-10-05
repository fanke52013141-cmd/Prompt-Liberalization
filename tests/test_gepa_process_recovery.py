"""Real child-service crash/restart recovery for the optional pinned GEPA SDK."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import setup_project_with_data, start_run
from prompt_lib.db import get_db
from prompt_lib.providers import MockProvider


pytestmark = pytest.mark.skipif(importlib.util.find_spec("gepa") is None,
                                reason="可选GEPA扩展未安装")


def test_gepa_recovers_after_real_service_kill_during_reflection(client):
    data = setup_project_with_data(client)
    seed = start_run(client, data["pid"], data["prompt_id"], data["rubric_id"],
                     dev_ids=data["item_ids"][:2], max_candidates=0)
    db = get_db()
    reached, release = threading.Event(), threading.Event()
    transport = {"calls": 0, "optimizer_calls": 0, "response": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            role = body["model"].removeprefix("local-")
            result = MockProvider({}).complete(role, body["model"], body["messages"], body,
                                               "offline-gepa-process")
            transport["calls"] += 1
            if role == "optimizer":
                transport["optimizer_calls"] += 1
            response = {"choices": [{"message": {"content": result.text},
                                     "finish_reason": result.finish}],
                        "usage": {"prompt_tokens": result.usage["in"],
                                  "completion_tokens": result.usage["out"]}}
            if role == "optimizer" and not reached.is_set():
                transport["response"] = result
                reached.set()
                if not release.wait(30):
                    return
            content = json.dumps(response, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    vendor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    vendor.daemon_threads = True
    vendor_thread = threading.Thread(target=vendor.serve_forever, daemon=True)
    vendor_thread.start()
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}/workflow-api/v1"
    command = [sys.executable, str(root / "run_server.py"), "--db", db.path,
               "--port", str(port)]
    sdk_root = Path(importlib.util.find_spec("gepa").origin).parent.parent
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8",
                   "PYTHONPATH": os.pathsep.join((str(sdk_root), os.environ.get("PYTHONPATH", "")))}
    service = None

    def request(method, path, body=None, timeout=10):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=encoded, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)

    def launch():
        process = subprocess.Popen(command, cwd=root, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError("GEPA child service exited during startup")
            try:
                request("GET", f"/projects/{data['pid']}", timeout=1)
                return process
            except OSError:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait(10)
                    raise AssertionError("GEPA child service failed to start")
                time.sleep(0.05)

    try:
        client.put("/workflow-api/v1/settings/connections", json={
            "id": "conn_gepa_process", "name": "GEPA crash-test loopback",
            "provider": "openai_compat", "base_url": f"http://127.0.0.1:{vendor.server_port}/v1",
            "model": "local-generation", "api_key": "offline-only"})
        service = launch()
        draft = seed["snapshot"]
        draft["data"]["select_item_ids"] = data["item_ids"][8:10]
        draft["models"] = {role: {"connection_id": "conn_gepa_process", "model": f"local-{role}"}
                           for role in ("generation", "evaluation", "optimizer")}
        draft["optimization"].update({"strategy": "gepa", "gepa_max_metric_calls": 8,
                                      "dev_sample_size": 2, "min_delta": 0.02,
                                      "human_in_loop": False})
        accepted = request("POST", f"/projects/{data['pid']}/runs", draft)
        rid = accepted["id"]
        assert reached.wait(20), "GEPA did not reach its first reflection request"
        active = request("GET", f"/runs/{rid}")
        assert active["state"] == "running" and active["stage"] == "gepa_search", active
        checkpoint = Path(db.path).parent / ".gepa" / rid / "gepa_state.bin"
        assert checkpoint.is_file(), "SDK checkpoint was not persisted before reflection"

        service.kill()
        service.wait(10)
        service = None
        release.set()
        service = launch()
        recovered = request("GET", f"/runs/{rid}")
        assert recovered["state"] == "paused_interrupted", recovered
        attempts = request("GET", f"/runs/{rid}/ledger/attempts")["attempts"]
        unknown = [attempt for attempt in attempts if attempt["status"] == "sent_unknown"]
        assert len(unknown) == 1 and unknown[0]["role"] == "optimizer", unknown

        original = transport["response"]
        response = request("POST", f"/runs/{rid}/ledger/{unknown[0]['attempt_id']}/reconcile-response", {
            "request_hash": unknown[0]["request_hash"],
            "reason": "进程中断后核对隔离回环供应商保留的原始反思响应",
            "evidence": "测试HTTP服务记录的同一在途请求与响应",
            "text": original.text, "finish": original.finish,
            "actual_in": original.usage["in"], "actual_out": original.usage["out"]})
        assert response["pending_attempts"] == 0 and response["consistent"] is True, response
        request("POST", f"/runs/{rid}/resume")
        deadline = time.monotonic() + 45
        final = request("GET", f"/runs/{rid}")
        while final["state"] not in ("completed", "failed", "cancelled", "paused_budget", "paused_interrupted"):
            assert time.monotonic() < deadline, final
            time.sleep(0.05)
            final = request("GET", f"/runs/{rid}")
        assert final["state"] == "completed", final.get("error")
        assert transport["optimizer_calls"] == 1, "Reconciled reflection was physically replayed"
        duplicate_ids = db.query(
            "SELECT logical_id FROM ledger WHERE run_id=? GROUP BY logical_id HAVING COUNT(*)>1", (rid,))
        assert not duplicate_ids, duplicate_ids
        assert request("GET", f"/runs/{rid}")["snapshot"]["package_lock"]["gepa"] == "0.1.4"
    finally:
        release.set()
        if service is not None and service.poll() is None:
            service.kill()
            service.wait(10)
        vendor.shutdown()
        vendor.server_close()
        vendor_thread.join(10)
