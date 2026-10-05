"""Real child service termination against an owned loopback model transport."""
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

from .conftest import setup_project_with_data, start_run
from prompt_lib.db import get_db
from prompt_lib.providers import MockProvider


def test_killed_service_recovers_partial_exam_and_reuses_confirmed_model_result(client, tmp_path):
    data = setup_project_with_data(client)
    seed = start_run(client, data['pid'], data['prompt_id'], data['rubric_id'],
                     dev_ids=data['item_ids'][:2], max_candidates=0)
    db = get_db()
    reached, release = threading.Event(), threading.Event()
    transport = {'calls': 0, 'gate_at': None, 'result': None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            role = body['model'].removeprefix('local-')
            result = MockProvider({}).complete(role, body['model'], body['messages'], body, 'offline-transport')
            transport['calls'] += 1
            response = {'choices': [{'message': {'content': result.text}, 'finish_reason': result.finish}],
                        'usage': {'prompt_tokens': result.usage['in'], 'completion_tokens': result.usage['out']}}
            if transport['calls'] == transport['gate_at']:
                transport['result'] = result
                reached.set()
                if not release.wait(30):
                    return
            content = json.dumps(response, ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    vendor = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    vendor.daemon_threads = True
    vendor_thread = threading.Thread(target=vendor.serve_forever, daemon=True)
    vendor_thread.start()
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    base = f'http://127.0.0.1:{port}/workflow-api/v1'
    command = [sys.executable, str(root / 'run_server.py'), '--db', db.path, '--port', str(port)]
    service = None
    caller = None
    caller_errors = []

    def request(method, path, body=None, timeout=10):
        encoded = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=encoded, method=method, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)

    def launch():
        process = subprocess.Popen(command, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError('Owned child service exited during startup')
            try:
                request('GET', f"/projects/{data['pid']}", timeout=1)
                return process
            except OSError:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait(10)
                    raise AssertionError('Owned child service failed to start')
                time.sleep(0.05)

    try:
        client.put('/workflow-api/v1/settings/connections', json={
            'id': 'conn_process_test', 'name': 'Owned loopback transport', 'provider': 'openai_compat',
            'base_url': f'http://127.0.0.1:{vendor.server_port}/v1', 'model': 'local-generation', 'api_key': 'offline-only'})
        service = launch()
        draft = seed['snapshot']
        draft['models'] = {role: {'connection_id': 'conn_process_test', 'model': f'local-{role}'}
                           for role in ('generation', 'evaluation', 'optimizer')}
        run = request('POST', f"/projects/{data['pid']}/runs", draft)
        rid = run['id']
        deadline = time.monotonic() + 15
        while request('GET', f'/runs/{rid}')['state'] != 'completed':
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert transport['calls'] == 4
        request('POST', f'/runs/{rid}/lock', {'candidate_id': 'baseline'})
        transport['gate_at'] = transport['calls'] + 5

        def begin_exam():
            try:
                request('POST', f'/runs/{rid}/accept', timeout=30)
            except OSError as exc:
                caller_errors.append(exc)

        caller = threading.Thread(target=begin_exam)
        caller.start()
        assert reached.wait(15)
        job = dict(db.one('SELECT * FROM acceptance_jobs WHERE run_id=?', (rid,)))
        prior_outputs = {row['id'] for row in db.query('SELECT id FROM outputs WHERE run_id=? AND item_id IN (SELECT item_id FROM sealed_artifacts)', (rid,))}
        assert len(prior_outputs) == 2
        assert db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance' AND status='reserved'", (rid,))[0] == 1
        service.kill()
        service.wait(10)
        caller.join(10)
        assert not caller.is_alive() and caller_errors
        release.set()
        service = launch()
        recovered = request('GET', f'/runs/{rid}/acceptance-job')['job']
        assert recovered['id'] == job['id'] and recovered['state'] == 'paused_interrupted'
        pending = db.one("SELECT * FROM ledger WHERE run_id=? AND status='sent_unknown'", (rid,))
        assert pending and pending['reserved_tokens'] > 0
        assert db.one('SELECT manifest_hash FROM acceptance_jobs WHERE id=?', (job['id'],))[0] == job['manifest_hash']
        result = transport['result']
        request('POST', f"/runs/{rid}/ledger/{pending['attempt_id']}/reconcile-response", {
            'request_hash': pending['request_hash'], 'reason': '恢复进程中断时的响应', 'evidence': '本地供应商保留的实际请求记录',
            'text': result.text, 'finish': result.finish, 'actual_in': result.usage['in'], 'actual_out': result.usage['out']})
        report = request('POST', f'/runs/{rid}/accept')
        assert report['acceptance_id'] == job['id']
        assert request('POST', f'/runs/{rid}/accept')['id'] == report['id']
        assert transport['calls'] == 12  # Four baseline + eight exam physical requests, including the interrupted one.
        assert db.one("SELECT COUNT(*) FROM ledger WHERE run_id=? AND phase='acceptance'", (rid,))[0] == 8
        outputs = {row['id'] for row in db.query('SELECT id FROM outputs WHERE run_id=? AND item_id IN (SELECT item_id FROM sealed_artifacts)', (rid,))}
        assert len(outputs) == 4 and prior_outputs <= outputs
        assert db.one('SELECT COUNT(*) FROM acceptance_reports WHERE run_id=?', (rid,))[0] == 1
    finally:
        release.set()
        if service is not None and service.poll() is None:
            service.kill()
            service.wait(10)
        if caller is not None:
            caller.join(10)
        vendor.shutdown()
        vendor.server_close()
        vendor_thread.join(10)
