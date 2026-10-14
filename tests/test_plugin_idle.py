import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SERVER = PROJECT / "plugins/codex-trace-probe/server.py"
spec = importlib.util.spec_from_file_location("idle_trace_server", SERVER)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
A = "11111111-2222-4333-8444-555555555555"
B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
V = "bbbbbbbb-1111-4222-8333-444444444444"
W = "cccccccc-1111-4222-8333-444444444444"


class IdleTraceTests(unittest.TestCase):
    def setUp(self):
        self.store = plugin.TraceStore()
        self.addCleanup(self.store.close)

    def test_snapshot_lease_is_short_but_release_guard_keeps_five_minutes(self):
        self.assertEqual(plugin.IDLE_TTL, 30)
        self.assertEqual(plugin.SWEEP_INTERVAL, 5)
        self.assertEqual(plugin.RELEASE_GUARD_TTL, 300)

    def test_late_request_is_rejected_after_idle_ttl_until_guard_expiry(self):
        now = time.monotonic()
        with patch.object(plugin.time, "monotonic", return_value=now):
            self.store.release(A, V)
        self.assertEqual(self.store.released[V], (A, now + 300))
        for elapsed in (31, 299.999):
            with patch.object(plugin.time, "monotonic", return_value=now + elapsed):
                with self.assertRaisesRegex(ValueError, "released"):
                    self.store.view(A, V)
                with self.assertRaisesRegex(ValueError, "another thread"):
                    self.store.release(B, V)
        with patch.object(plugin.time, "monotonic", return_value=now + 300):
            self.store.view(A, V)
        self.assertNotIn(V, self.store.released)

    def test_discovery_does_not_load_snapshot_modules_or_start_cleanup(self):
        code = """import importlib.util, json, sys, threading
spec = importlib.util.spec_from_file_location('discovery_fixture', sys.argv[1])
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
server = plugin.ProbeServer()
for method in ('initialize', 'tools/list', 'resources/list', 'resources/read'):
    response = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method,
        'params': {'uri': plugin.TRACE_RESOURCE}})
    assert 'error' not in response, response
print(json.dumps({'loaded': [name for name in ('base64', 'datetime', 'hashlib',
    'subprocess', 'tempfile', 'uuid') if name in sys.modules],
    'worker': server.traces.worker is not None, 'threads': threading.active_count()}))
server.traces.close()
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, str(SERVER)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"loaded": [], "worker": False, "threads": 1})

    def test_cleanup_starts_with_first_view_and_stops_after_last_release(self):
        self.assertIsNone(self.store.worker)
        self.store.start()
        self.assertIsNone(self.store.worker)
        self.store.view(A, V)
        worker = self.store.worker
        self.assertIsNotNone(worker)
        self.assertTrue(worker.is_alive())
        self.store.view(B, W)
        self.assertTrue(self.store.release(A, V))
        self.assertIs(self.store.worker, worker)
        self.assertTrue(self.store.release(B, W))
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.store.worker)
        self.assertEqual(set(self.store.released), {V, W})

    def test_reopen_starts_cleanup_and_keeps_released_guard_owner(self):
        self.store.view(A, V)
        worker = self.store.worker
        self.store.release(A, V)
        if worker is not None:
            worker.join(1)
        with self.assertRaisesRegex(ValueError, "released"):
            self.store.view(A, V)
        with self.assertRaisesRegex(ValueError, "another thread"):
            self.store.release(B, V)
        self.store.view(B, W)
        self.assertIsNotNone(self.store.worker)
        self.assertTrue(self.store.worker.is_alive())
        self.assertEqual(self.store.views[W]["thread_id"], B)
        self.assertEqual(self.store.released[V][0], A)

    def test_main_discovery_does_not_eagerly_start_cleanup(self):
        code = """import importlib.util, io, json, sys
spec = importlib.util.spec_from_file_location('main_fixture', sys.argv[1])
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
def unexpected_start(self):
    raise RuntimeError('cleanup started before a page request')
plugin.TraceStore.start = unexpected_start
sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\\n'))
plugin.main()
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, str(SERVER)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tools", json.loads(result.stdout)["result"])

    def test_guard_only_store_stays_idle_and_expires_guards_on_request(self):
        self.store.release(A, V)
        self.assertIsNone(self.store.worker)
        expires = self.store.released[V][1]
        with patch.object(plugin, "MAX_RELEASE_GUARDS", 1):
            with self.assertRaisesRegex(ValueError, "lifecycle limit"):
                self.store.release(B, W)
            with patch.object(plugin.time, "monotonic", return_value=expires):
                self.assertFalse(self.store.release(B, W))
        self.assertEqual(set(self.store.released), {W})
        self.assertIsNone(self.store.worker)

    def test_last_page_expires_without_close_notification_and_worker_exits(self):
        with patch.object(plugin, "SWEEP_INTERVAL", 0.01):
            self.store.view(A, V)
            worker = self.store.worker
            with self.store.lock:
                self.store.views[V]["seen"] = time.monotonic() - plugin.IDLE_TTL - 1
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.views, {})
        self.assertIsNone(self.store.worker)

    def test_last_release_racing_new_page_always_retains_live_cleanup(self):
        for index in range(20):
            old, new = "old-" + str(index), "new-" + str(index)
            self.store.view(A, old)
            barrier, errors = threading.Barrier(3), []
            def invoke(operation):
                try:
                    barrier.wait(1)
                    operation()
                except Exception as error:
                    errors.append(error)
            releaser = threading.Thread(target=invoke, args=(lambda: self.store.release(A, old),))
            opener = threading.Thread(target=invoke, args=(lambda: self.store.view(B, new),))
            releaser.start()
            opener.start()
            barrier.wait(1)
            releaser.join(1)
            opener.join(1)
            self.assertFalse(releaser.is_alive() or opener.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(set(self.store.views), {new})
            worker = self.store.worker
            self.assertTrue(worker.is_alive())
            self.store.release(B, new)
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_cleanup_start_failure_does_not_leave_unmonitored_page(self):
        with patch.object(plugin.threading.Thread, "start", side_effect=RuntimeError("thread capacity")):
            with self.assertRaisesRegex(OSError, "Could not start trace cleanup thread"):
                self.store.view(A, V)
        self.assertEqual(self.store.views, {})
        self.assertIsNone(self.store.worker)
        self.store.view(A, V)
        self.assertTrue(self.store.worker.is_alive())

    def test_cleanup_start_failure_is_rpc_error_and_next_request_can_retry(self):
        server = plugin.ProbeServer()
        self.addCleanup(server.traces.close)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "trace_snapshot", "arguments": {"view_id": V, "request_nonce": W},
            "_meta": {"thread_id": A}}}
        with patch.object(plugin.threading.Thread, "start", side_effect=RuntimeError("thread capacity")), \
                patch.object(plugin.sys, "stderr", io.StringIO()):
            response = server.dispatch(request)
        self.assertEqual(response["error"]["code"], -32603)
        self.assertIn("Could not start trace cleanup thread", response["error"]["message"])
        self.assertEqual(server.traces.views, {})
        self.assertEqual(server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"], {})
        server.traces.view(A, V)
        self.assertTrue(server.traces.worker.is_alive())

    def test_cleanup_logs_os_error_then_retries_and_exits(self):
        self.store.view(A, V)
        worker, original, attempts = self.store.worker, self.store.sweep, []
        def interrupted_sweep():
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("temporary cleanup failure")
            return original()
        log = io.StringIO()
        with patch.object(plugin, "SWEEP_INTERVAL", 0.01), \
                patch.object(self.store, "sweep", side_effect=interrupted_sweep), \
                patch.object(plugin.sys, "stderr", log):
            with self.store.lock:
                self.store.views[V]["seen"] = time.monotonic() - plugin.IDLE_TTL - 1
                self.store.start()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(json.loads(log.getvalue()), {"event": "trace_cleanup_error", "type": "OSError"})
        self.assertEqual(self.store.views, {})

    def test_close_racing_first_page_leaves_no_worker_or_view(self):
        for index in range(20):
            store = plugin.TraceStore()
            self.addCleanup(store.close)
            barrier, errors = threading.Barrier(3), []
            def open_page():
                try:
                    barrier.wait(1)
                    store.view(A, V)
                except ValueError as error:
                    if str(error) != "Trace store is closed":
                        errors.append(error)
                except Exception as error:
                    errors.append(error)
            def close_store():
                try:
                    barrier.wait(1)
                    store.close()
                except Exception as error:
                    errors.append(error)
            opener = threading.Thread(target=open_page)
            closer = threading.Thread(target=close_store)
            opener.start()
            closer.start()
            barrier.wait(1)
            opener.join(1)
            closer.join(1)
            self.assertFalse(opener.is_alive() or closer.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(store.closed)
            self.assertEqual(store.views, {})
            self.assertIsNone(store.worker)


if __name__ == "__main__":
    unittest.main()
