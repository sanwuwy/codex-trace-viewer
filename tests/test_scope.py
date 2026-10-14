from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location("scope_server", Path(__file__).resolve().parents[1] / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"
        self.archive = self.root / "archived_sessions"
        self.sessions.mkdir()
        self.archive.mkdir()
        self.settings = patch.multiple(server, ROOTS=[str(self.sessions), str(self.archive)],
            CACHE_PATH=str(self.root / "cache.json"), INDEX_PATH=str(self.root / "index"),
            STATE_PATH=str(self.root / "state"), _cache={"peek": {}, "stats": {}},
            _cache_loaded=False, _cache_dirty=False)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.a = self.write("unrelated-name.jsonl", "alpha")
        self.b = self.write("rollout-2026-10-10T02-00-00-beta.jsonl", "beta")

    def write(self, name, thread_id):
        path = self.sessions / name
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread_id}}) + "\n",
                        encoding="utf-8")
        return path

    def listen(self, thread_id, token="owned-secret"):
        scope = server.ThreadScope(thread_id) if thread_id else None
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        http.scope = scope
        http.instance_id = "instance-" + (thread_id or "generic")
        http.control_token = token
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(http.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(http.shutdown)
        return "http://127.0.0.1:%d" % http.server_address[1], worker

    def get(self, base, route):
        with urlopen(base + route, timeout=2) as response:
            return response.read().decode("utf-8")

    def post(self, base, route, data=None, token=None):
        body = json.dumps(data).encode() if data is not None else b""
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        with urlopen(Request(base + route, data=body, headers=headers), timeout=2) as response:
            return json.load(response)

    def test_two_services_keep_immutable_scope_and_reject_other_raw_and_stats(self):
        a, _ = self.listen("alpha")
        b, _ = self.listen("beta")
        for base, thread_id, own, other in ((a, "alpha", self.a, self.b), (b, "beta", self.b, self.a)):
            data = json.loads(self.get(base, "/api/sessions?thread=other&limit=0"))
            self.assertEqual(data["bound_thread"], thread_id)
            self.assertEqual([row["thread_id"] for row in data["items"]], [thread_id])
            self.assertTrue(data["items"][0]["revision"])
            self.assertEqual(self.get(base, "/api/raw?" + urlencode({"path": str(own)})), own.read_text())
            for route in ("/api/raw?" + urlencode({"path": str(other)}),):
                with self.assertRaises(HTTPError) as denied:
                    self.get(base, route)
                self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(HTTPError) as denied:
                self.post(base, "/api/stats", {"path": str(other), "stats": {"turns": 99}})
            self.assertEqual(denied.exception.code, 403)

    def test_missing_transcript_waits_without_using_filename_or_another_thread(self):
        self.write("rollout-2026-10-10T02-00-00-missing.jsonl", "beta")
        base, _ = self.listen("missing")
        data = json.loads(self.get(base, "/api/sessions"))
        self.assertEqual(data["items"], [])
        self.assertEqual(data["bound_thread"], "missing")
        self.write("new-record.jsonl", "missing")
        self.assertEqual(json.loads(self.get(base, "/api/sessions"))["count"], 1)

    def test_revision_changes_on_append_and_scope_survives_archive_move(self):
        base, _ = self.listen("alpha")
        old = json.loads(self.get(base, "/api/sessions"))["items"][0]["revision"]
        with self.a.open("a") as output:
            output.write('{"type":"response_item","payload":{}}\n')
        new = json.loads(self.get(base, "/api/sessions"))["items"][0]["revision"]
        self.assertNotEqual(old, new)
        archived = self.archive / self.a.name
        self.a.rename(archived)
        row = json.loads(self.get(base, "/api/sessions"))["items"][0]
        self.assertEqual(row["path"], str(archived.resolve()))
        self.assertNotEqual(row["revision"], new)

    def test_replaced_file_cannot_expose_another_threads_content(self):
        base, _ = self.listen("alpha")
        self.get(base, "/api/sessions")
        self.write(self.a.name, "beta")
        self.assertEqual(json.loads(self.get(base, "/api/sessions"))["items"], [])
        with self.assertRaises(HTTPError) as denied:
            self.get(base, "/api/raw?" + urlencode({"path": str(self.a)}))
        self.assertEqual(denied.exception.code, 403)

    def test_incomplete_utf8_append_does_not_hide_a_complete_session_header(self):
        self.a.write_bytes(self.a.read_bytes() + b'{"text":"\xe4\xb8')
        rows = server.ThreadScope("alpha").items()
        self.assertEqual([row["thread_id"] for row in rows], ["alpha"])
        self.a.write_bytes(b'{"type":"session_meta","payload":{"id":"alpha","cwd":"\xe4\xb8')
        self.assertEqual(server.ThreadScope("alpha").items(), [])

    def test_scoped_statistics_never_overwrite_shared_cache(self):
        base, _ = self.listen("alpha")
        before = json.dumps(server._cache, sort_keys=True)
        self.post(base, "/api/stats", {"path": str(self.a), "stats": {"turns": 2}})
        self.assertEqual(json.loads(self.get(base, "/api/sessions"))["items"][0]["stats"], {"turns": 2})
        self.assertEqual(json.dumps(server._cache, sort_keys=True), before)
        self.assertFalse(Path(server.CACHE_PATH).exists())

    def test_raw_rechecks_identity_after_the_file_read(self):
        base, _ = self.listen("alpha")
        original = server.read_payload
        @contextmanager
        def replaced_payload(path):
            with original(self.b.resolve()) as snapshot:
                yield snapshot
        with patch.object(server, "read_payload", side_effect=replaced_payload):
            with self.assertRaises(HTTPError) as denied:
                self.get(base, "/api/raw?" + urlencode({"path": str(self.a)}))
        self.assertEqual(denied.exception.code, 403)

    def test_html_binding_and_health_use_server_identity_not_url_query(self):
        page = self.root / "index.html"
        page.write_text('<script type="application/json" id="viewer-config">{"thread_id":null}</script>')
        base, _ = self.listen("alpha")
        with patch.object(server, "INDEX", str(page)):
            html = self.get(base, "/?thread=beta")
        self.assertIn('"thread_id": "alpha"', html)
        health = json.loads(self.get(base, "/api/health"))
        self.assertEqual((health["app"], health["protocol"], health["thread_id"]),
                         ("codex-trace-viewer", 1, "alpha"))
        self.assertEqual(health["instance_id"], "instance-alpha")
        self.assertNotIn("owned-secret", json.dumps(health))

    def test_shutdown_authentication_stops_only_the_owned_service(self):
        a, worker = self.listen("alpha")
        b, _ = self.listen("beta", "other-secret")
        for token in (None, "wrong", "other-secret", "caf\u00e9"):
            with self.assertRaises(HTTPError) as denied:
                self.post(a, "/api/shutdown", token=token)
            self.assertEqual(denied.exception.code, 403)
        self.assertTrue(worker.is_alive())
        self.assertTrue(self.post(a, "/api/shutdown", token="owned-secret")["ok"])
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(json.loads(self.get(b, "/api/health"))["thread_id"], "beta")

    def test_generic_mode_still_lists_all_records(self):
        base, _ = self.listen(None)
        data = json.loads(self.get(base, "/api/sessions"))
        self.assertIsNone(data["bound_thread"])
        self.assertEqual(data["count"], 2)

    def test_scoped_poll_does_not_reopen_unchanged_files_or_load_global_indexes(self):
        scope = server.ThreadScope("alpha")
        scope.items()
        with patch.object(server, "open", wraps=open, create=True) as reads, \
             patch.object(server.os, "walk", side_effect=AssertionError("unexpected directory scan")), \
             patch.object(server, "load_thread_meta", side_effect=AssertionError("unexpected global index")), \
             patch.object(server, "resolve_project", side_effect=AssertionError("unexpected project lookup")):
            for _ in range(5):
                self.assertEqual(scope.items()[0]["thread_id"], "alpha")
            reads.assert_not_called()

    def test_complete_metadata_does_not_read_a_large_body(self):
        with self.a.open("ab") as output:
            output.write(b"x" * server.HEAD_BYTES * 2)
        positions = []
        class TrackingFile:
            def __init__(self, path, mode):
                self.file = open(path, mode)
            def __enter__(self):
                return self.file
            def __exit__(self, *args):
                positions.append(self.file.tell())
                self.file.close()
        with patch.object(server, "open", side_effect=TrackingFile, create=True):
            self.assertEqual(server.ThreadScope("alpha").items()[0]["thread_id"], "alpha")
        self.assertTrue(positions)
        self.assertLess(max(positions), 2048)

    def test_waiting_reuses_directory_and_header_cache_until_the_deadline(self):
        scope = server.ThreadScope("missing")
        with patch("time.monotonic", return_value=100) as clock, \
             patch.object(server.os, "walk", wraps=server.os.walk) as walk, \
             patch.object(server, "open", wraps=open, create=True) as reads:
            self.assertEqual(scope.items(), [])
            initial_walks, initial_reads = walk.call_count, reads.call_count
            clock.return_value = 102
            self.assertEqual(scope.items(), [])
            self.assertEqual(walk.call_count, initial_walks)
            self.assertEqual(reads.call_count, initial_reads)
            clock.return_value = 111
            self.assertEqual(scope.items(), [])
            self.assertGreater(walk.call_count, initial_walks)
            self.assertEqual(reads.call_count, initial_reads)

    def test_new_nested_transcript_invalidates_waiting_cache_immediately(self):
        scope = server.ThreadScope("missing")
        with patch("time.monotonic", return_value=100) as clock:
            self.assertEqual(scope.items(), [])
            day = self.sessions / "new" / "day"
            day.mkdir(parents=True)
            path = day / "arbitrary-name.jsonl"
            path.write_text(json.dumps({"type":"session_meta", "payload":{"id":"missing"}}) + "\n")
            stat = self.sessions.stat()
            os.utime(self.sessions, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
            clock.return_value = 101
            self.assertEqual(scope.items()[0]["path"], str(path.resolve()))

    def test_in_place_header_changes_are_found_by_periodic_rescan(self):
        scope = server.ThreadScope("missing")
        with patch("time.monotonic", return_value=100) as clock:
            self.assertEqual(scope.items(), [])
            self.write(self.b.name, "missing")
            clock.return_value = 101
            self.assertEqual(scope.items(), [])
            clock.return_value = 111
            self.assertEqual(scope.items()[0]["thread_id"], "missing")

    def test_initial_duplicate_ids_remain_an_error_with_arbitrary_names(self):
        self.write("second-arbitrary-name.jsonl", "alpha")
        with self.assertRaisesRegex(ValueError, "多个"):
            server.ThreadScope("alpha").items()

    def test_header_cache_does_not_retain_other_conversations_content(self):
        self.b.write_text(json.dumps({"type":"session_meta", "payload":{"id":"beta", "private":"other-content"}}) + "\n")
        scope = server.ThreadScope("alpha")
        scope.items()
        self.assertNotIn("other-content", repr(scope.headers))

    def test_bound_thread_ids_reject_path_and_html_injection(self):
        for value in ("../alpha", "", "a" * 129, "</script>", "alpha beta"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.ThreadScope(value)


if __name__ == "__main__":
    unittest.main()
