import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lazy_trace_server", PROJECT / "plugins/codex-trace-probe/server.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
A = "11111111-2222-4333-8444-555555555555"
B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
V = "bbbbbbbb-1111-4222-8333-444444444444"
W = "cccccccc-1111-4222-8333-444444444444"


class LazyTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lazy trace ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = plugin.TraceStore()
        self.server = plugin.ProbeServer()
        self.addCleanup(self.store.close)
        self.addCleanup(self.server.traces.close)
        root = self.home / "sessions"
        root.mkdir()
        self.path = root / ("rollout-" + A + ".jsonl")
        self.records = [
            {"type": "session_meta", "payload": {"id": A}},
            {"type": "turn_context", "payload": {"turn_id": "t", "model": "fixture"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "中文🙂"}]}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "McpToolCall", "id": "c", "result": {"isError": True, "content": [{"type": "text", "text": "large" * 10000}]}}}},
            {"type": "compacted", "payload": {"message": "summary", "replacement_history": [{"body": "private historical content" * 10000}]}}
        ]
        self.save()

    def save(self):
        self.path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in self.records) + "\n", encoding="utf-8")

    def payload(self, manifest, detail=False):
        return b"".join(base64.b64decode(self.store.chunk(A, V, manifest["snapshot_id"], index,
            manifest["detail_id"] if detail else None)["data"]) for index in range(manifest["total_chunks"]))

    def call(self, name, **arguments):
        return self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": {"view_id": V, "request_nonce": W, **arguments}, "_meta": {"thread_id": A}}})

    def test_outline_is_small_but_details_roundtrip_original_records(self):
        snapshot = self.store.snapshot(A, V, None, "outline")
        self.assertEqual(snapshot["format"], "outline")
        self.assertEqual(snapshot["source_bytes"], self.path.stat().st_size)
        self.assertLess(snapshot["total_bytes"], snapshot["source_bytes"] // 100)
        envelopes = [json.loads(line) for line in self.payload(snapshot).splitlines()]
        self.assertEqual([entry["index"] for entry in envelopes], list(range(5)))
        self.assertEqual(snapshot["record_count"], 5)
        self.assertEqual(envelopes[3]["record"]["payload"]["item"]["result"], {"isError": True})
        self.assertNotIn("replacement_history", envelopes[4]["record"]["payload"])
        detail = self.store.detail(A, V, snapshot["snapshot_id"], [4, 2, 3])
        self.assertEqual(detail["record_count"], 3)
        restored = [json.loads(line) for line in self.payload(detail, True).splitlines()]
        self.assertEqual(restored, [{"index": index, "record": self.records[index]} for index in [4, 2, 3]])
        self.assertEqual(detail["revision"], snapshot["session"]["revision"])
        with patch.object(plugin, "open_record", side_effect=AssertionError("unchanged source read")):
            same = self.store.snapshot(A, V, snapshot["session"]["revision"], "outline")
        self.assertEqual(same["status"], "unchanged")
        self.assertEqual(same["total_chunks"], 0)

    def test_projection_preserves_classification_arguments_and_execution_status(self):
        output = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "Script failed\nfull output"}}
        self.assertEqual(plugin.project_record(output.copy()), output)
        command = {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "status": "failed", "exit_code": 1, "duration": "1s", "command": ["false"], "error": "failure", "stdout": "x", "stderr": "y", "aggregated_output": "z", "formatted_output": "w"}}}
        item = plugin.project_record(command)["payload"]["item"]
        self.assertEqual(item, {"type": "CommandExecution", "status": "failed", "exit_code": 1, "duration": "1s", "command": ["false"], "error": "failure"})

    def test_invalid_lines_keep_source_indices_and_are_reported(self):
        with self.path.open("ab") as stream:
            stream.write(b'\nnot json\n{"type":"response_item","payload":{"output":NaN}}\n')
            stream.write(b'{"type":"event_msg","payload":{"type":"task_complete"}}')
        snapshot = self.store.snapshot(A, V, None, "outline")
        envelopes = [json.loads(line) for line in self.payload(snapshot).splitlines()]
        self.assertEqual([entry["index"] for entry in envelopes], [0, 1, 2, 3, 4, 8])
        self.assertEqual(snapshot["skipped_records"], 2)
        self.assertEqual(snapshot["record_count"], 6)
        detail = self.store.detail(A, V, snapshot["snapshot_id"], [8])
        self.assertEqual(json.loads(self.payload(detail, True))["index"], 8)
        for indices in ([5], [6], [7], [-1], [True], [1.5], [plugin.MAX_SAFE_INTEGER + 1], [1, 1], [], "1", [0] * 8193):
            with self.subTest(indices=str(indices)[:30]):
                with self.assertRaises(ValueError):
                    self.store.detail(A, V, snapshot["snapshot_id"], indices)
        self.assertEqual(json.loads(self.payload(detail, True))["index"], 8)

    def test_outline_whitespace_crlf_unicode_and_eof_keep_physical_line_semantics(self):
        records = [
            {"type": "session_meta", "payload": {"id": A}},
            {"type": "event_msg", "payload": {"text": "中文🙂"}},
            {"type": "event_msg", "payload": {"text": "EOF"}},
        ]
        self.path.write_bytes((json.dumps(records[0]) + "\r\n \t\r\n" +
            " \t" + json.dumps(records[1], ensure_ascii=False) + " \t\r\n" +
            "{} trailing\n\u2003{}\u2003\n " + json.dumps(records[2]) + "\t").encode("utf-8"))
        snapshot = self.store.snapshot(A, V, None, "outline")
        envelopes = [json.loads(line) for line in self.payload(snapshot).splitlines()]
        self.assertEqual(envelopes, [{"index": index, "record": record} for index, record in zip([0, 2, 5], records)])
        self.assertEqual(snapshot["record_count"], 3)
        self.assertEqual(snapshot["skipped_records"], 2)

    def test_detail_replacement_and_snapshot_refresh_close_old_payloads(self):
        first = self.store.snapshot(A, V, None, "outline")
        detail = self.store.detail(A, V, first["snapshot_id"], [3])
        replaced_detail = self.store.views[V]["detail_file"]
        self.store.detail(A, V, first["snapshot_id"], [2])
        self.assertTrue(replaced_detail.closed)
        with self.assertRaises(ValueError):
            self.store.chunk(A, V, first["snapshot_id"], 0, detail["detail_id"])
        old_files = [self.store.views[V][key] for key in ("snapshot_file", "outline_file", "detail_file")]
        self.records.append({"type": "event_msg", "payload": {"type": "task_complete"}})
        self.save()
        second = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(second["status"], "delta")
        self.assertEqual(second["start_offset"], first["total_bytes"])
        self.assertEqual(second["base_revision"], first["session"]["revision"])
        self.assertTrue(all(not file.closed for file in old_files[:2]))
        self.assertTrue(old_files[2].closed)
        self.assertIsNone(self.store.views[V]["detail_id"])
        with self.assertRaises(ValueError):
            self.store.detail(A, V, first["snapshot_id"], [2])
        self.assertEqual(len(self.payload(second).splitlines()), 1)

    def test_format_switch_same_revision_still_returns_ready_and_invalidates_chunks(self):
        full = self.store.snapshot(A, V, None)
        outline = self.store.snapshot(A, V, full["session"]["revision"], "outline")
        self.assertEqual(outline["status"], "ready")
        self.assertEqual(outline["session"]["revision"], full["session"]["revision"])
        with self.assertRaises(ValueError):
            self.store.chunk(A, V, full["snapshot_id"], 0)
        restored = self.store.snapshot(A, V, outline["session"]["revision"])
        self.assertEqual(restored["status"], "ready")
        self.assertEqual(self.payload(restored), self.path.read_bytes())
        with self.assertRaises(ValueError):
            self.store.detail(A, V, restored["snapshot_id"], [0])

    def test_release_and_expiry_close_raw_outline_and_detail_independently(self):
        first = self.store.snapshot(A, V, None, "outline")
        self.store.detail(A, V, first["snapshot_id"], [4])
        second = self.store.snapshot(A, W, None, "outline")
        self.store.detail(A, W, second["snapshot_id"], [3])
        removed = [self.store.views[V][key] for key in ("snapshot_file", "outline_file", "detail_file")]
        kept = [self.store.views[W][key] for key in ("snapshot_file", "outline_file", "detail_file")]
        with self.assertRaises(ValueError):
            self.store.detail(B, V, first["snapshot_id"], [0])
        with self.assertRaises(ValueError):
            self.store.detail(A, W, first["snapshot_id"], [0])
        self.store.release(A, V)
        self.assertTrue(all(file.closed for file in removed))
        self.assertTrue(all(not file.closed for file in kept))
        self.store.views[W]["seen"] -= 301
        self.store.sweep()
        self.assertTrue(all(file.closed for file in kept))
        self.assertEqual(self.store.views, {})

    def test_rpc_exposes_bounded_detail_tool_and_rejects_bad_input_before_io(self):
        listing = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        definition = next(tool for tool in listing if tool["name"] == "trace_detail")
        self.assertEqual(definition["inputSchema"]["properties"]["record_indices"]["maxItems"], 8192)
        self.assertEqual(definition["_meta"]["ui"], {"visibility": ["app"]})
        snapshot = self.call("trace_snapshot", format="outline")["result"]["structuredContent"]
        detail = self.call("trace_detail", snapshot_id=snapshot["snapshot_id"], record_indices=[2])["result"]["structuredContent"]
        chunk = self.call("trace_chunk", snapshot_id=snapshot["snapshot_id"], chunk_index=0, detail_id=detail["detail_id"])["result"]["structuredContent"]
        self.assertEqual(chunk["detail_id"], detail["detail_id"])
        self.assertEqual(json.loads(base64.b64decode(chunk["data"]))["record"], self.records[2])
        for name, arguments in (("trace_snapshot", {"format": "unknown"}),
                ("trace_snapshot", {"format": {}}),
                ("trace_chunk", {"snapshot_id": V, "chunk_index": 0, "detail_id": "invalid"}),
                ("trace_detail", {"snapshot_id": V, "record_indices": [True]}),
                ("trace_detail", {"snapshot_id": V, "record_indices": [0, 0]}),
                ("trace_detail", {"snapshot_id": V, "record_indices": [[0]]}),
                ("trace_detail", {"snapshot_id": V, "record_indices": list(range(8193))})):
            with self.subTest(name=name, arguments=str(arguments)[:60]):
                self.assertEqual(self.call(name, **arguments)["error"]["code"], -32602)

    def test_failed_outline_write_preserves_previous_snapshot_and_closes_candidates(self):
        snapshot = self.store.snapshot(A, V, None, "outline")
        detail = self.store.detail(A, V, snapshot["snapshot_id"], [4])
        old_bytes = self.payload(detail, True)
        old_files = [self.store.views[V][key] for key in ("snapshot_file", "outline_file", "detail_file")]
        self.records.append({"type": "event_msg", "payload": {"type": "task_complete"}})
        self.save()
        raw = tempfile.TemporaryFile(mode="w+b")
        outline = tempfile.TemporaryFile(mode="w+b")
        metadata = tempfile.TemporaryFile(mode="w+b")
        def failed_worker(*arguments, **keywords):
            keywords["stdout"].write(b"partial outline")
            keywords["stderr"].write(b'{"error":"OSError"}')
            return SimpleNamespace(returncode=1)
        with patch.object(tempfile, "TemporaryFile", side_effect=[raw, outline, metadata]), \
                patch.object(subprocess, "run", side_effect=failed_worker):
            with self.assertRaisesRegex(OSError, "Outline worker failed: OSError"):
                self.store.snapshot(A, V, snapshot["session"]["revision"], "outline")
        self.assertTrue(raw.closed)
        self.assertTrue(outline.closed)
        self.assertTrue(metadata.closed)
        self.assertTrue(all(not file.closed for file in old_files))
        self.assertEqual(self.payload(detail, True), old_bytes)

    def test_failed_detail_write_or_short_read_preserves_previous_detail(self):
        snapshot = self.store.snapshot(A, V, None, "outline")
        detail = self.store.detail(A, V, snapshot["snapshot_id"], [3])
        old_bytes = self.payload(detail, True)
        old_file = self.store.views[V]["detail_file"]
        candidate = tempfile.TemporaryFile(mode="w+b")
        with patch.object(tempfile, "TemporaryFile", return_value=candidate), \
                patch.object(candidate, "write", return_value=0):
            with self.assertRaisesRegex(OSError, "Incomplete detail write"):
                self.store.detail(A, V, snapshot["snapshot_id"], [4])
        self.assertTrue(candidate.closed)
        self.assertFalse(old_file.closed)
        self.assertEqual(self.payload(detail, True), old_bytes)
        raw = self.store.views[V]["snapshot_file"]
        with patch.object(raw, "read", return_value=b""):
            with self.assertRaisesRegex(OSError, "Detail source read was incomplete"):
                self.store.detail(A, V, snapshot["snapshot_id"], [4])
        self.assertEqual(self.payload(detail, True), old_bytes)

    def test_detail_unicode_and_large_record_roundtrip_across_small_pages(self):
        with patch.object(plugin, "CHUNK_BYTES", 31):
            snapshot = self.store.snapshot(A, V, None, "outline")
            detail = self.store.detail(A, V, snapshot["snapshot_id"], [2, 3])
        self.assertGreater(detail["total_chunks"], 256)
        pieces = []
        for index in range(detail["total_chunks"]):
            piece = self.store.chunk(A, V, snapshot["snapshot_id"], index, detail["detail_id"])
            self.assertEqual(piece["byte_offset"], index * 31)
            self.assertEqual(piece["done"], index == detail["total_chunks"] - 1)
            pieces.append(base64.b64decode(piece["data"]))
        self.assertEqual([json.loads(line) for line in b"".join(pieces).splitlines()],
            [{"index": index, "record": self.records[index]} for index in [2, 3]])
        with self.assertRaises(ValueError):
            self.store.chunk(A, V, snapshot["snapshot_id"], detail["total_chunks"], detail["detail_id"])

    def test_store_close_releases_all_lazy_payloads(self):
        snapshot = self.store.snapshot(A, V, None, "outline")
        self.store.detail(A, V, snapshot["snapshot_id"], [4])
        files = [self.store.views[V][key] for key in ("snapshot_file", "outline_file", "detail_file")]
        self.store.start()
        worker = self.store.worker
        self.store.close()
        self.assertTrue(all(file.closed for file in files))
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.views, {})
        with self.assertRaises(ValueError):
            self.store.detail(A, V, snapshot["snapshot_id"], [4])

    def test_outline_worker_exits_and_keeps_large_json_allocations_out_of_parent(self):
        self.records[-1]["payload"]["replacement_history"] = [{"text": "x" * (12 * 1024 * 1024)}]
        self.save()
        self.records[-1]["payload"]["replacement_history"] = []
        tracemalloc.start()
        try:
            snapshot = self.store.snapshot(A, V, None, "outline")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 5 * 1024 * 1024)
        view = self.store.views[V]
        self.assertNotEqual(view["outline_worker_pid"], os.getpid())
        with self.assertRaises(ProcessLookupError):
            os.kill(view["outline_worker_pid"], 0)
        self.assertEqual(snapshot["record_count"], 5)

    def test_worker_mode_failure_is_explicit_and_does_not_start_mcp(self):
        result = subprocess.run([sys.executable, "-B", str(PROJECT / "plugins/codex-trace-probe/server.py"), "--outline-worker"],
            input=b"invalid non-seekable input", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr)["error"], "UnsupportedOperation")

    def test_worker_timeout_closes_every_candidate_and_preserves_snapshot(self):
        snapshot = self.store.snapshot(A, V, None, "outline")
        other = self.store.snapshot(A, W, None, "outline")
        other_file = self.store.views[W]["snapshot_file"]
        old_bytes = self.payload(snapshot)
        with self.path.open("ab") as stream:
            stream.write(b'{}\n')
        candidates = [tempfile.TemporaryFile(mode="w+b") for _ in range(3)]
        with patch.object(tempfile, "TemporaryFile", side_effect=candidates), \
                patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("outline", plugin.OUTLINE_TIMEOUT)):
            with self.assertRaisesRegex(OSError, "timed out"):
                self.store.snapshot(A, V, snapshot["session"]["revision"], "outline")
        self.assertTrue(all(file.closed for file in candidates))
        self.assertEqual(self.payload(snapshot), old_bytes)
        self.assertFalse(other_file.closed)
        self.assertIn("data", self.store.chunk(A, W, other["snapshot_id"], 0))

    def test_worker_partial_or_invalid_metadata_cannot_commit(self):
        snapshot = self.store.snapshot(A, V, None, "outline")
        old_bytes = self.payload(snapshot)
        with self.path.open("ab") as stream:
            stream.write(b'{}\n')
        for metadata in (b'{', b'[]', b'{}', b'{"offsets":[[0,0,-1]],"source_bytes":0,"total_bytes":0,"skipped_records":0}'):
            def invalid_worker(*arguments, **keywords):
                keywords["stderr"].write(metadata)
                return SimpleNamespace(returncode=0)
            with patch.object(subprocess, "run", side_effect=invalid_worker):
                with self.assertRaises((OSError, ValueError)):
                    self.store.snapshot(A, V, snapshot["session"]["revision"], "outline")
            self.assertEqual(self.payload(snapshot), old_bytes)

    def test_worker_stdout_disk_failure_returns_error_and_exits(self):
        code = """import importlib.util, sys
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('worker_fixture', sys.argv[1])
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
class FailedOutput:
    def write(self, block):
        raise OSError(28, 'fixture disk full')
    def flush(self):
        pass
sys.stdout = SimpleNamespace(buffer=FailedOutput(), flush=lambda: None)
sys.exit(plugin.outline_worker())
"""
        with self.path.open("rb") as source:
            result = subprocess.run([sys.executable, "-B", "-c", code, str(PROJECT / "plugins/codex-trace-probe/server.py")],
                stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr), {"error": "OSError"})

    @unittest.skipUnless(os.name == "posix", "Uses POSIX child liveness check")
    def test_real_timed_out_child_is_killed_and_reaped(self):
        original_run = subprocess.run
        pids = []
        def slow_worker(*arguments, **keywords):
            code = "import os,sys,time;sys.stderr.write(str(os.getpid()));sys.stderr.flush();time.sleep(10)"
            keywords["timeout"] = 0.2
            try:
                return original_run([sys.executable, "-B", "-c", code], **keywords)
            except subprocess.TimeoutExpired:
                keywords["stderr"].seek(0)
                pids.append(int(keywords["stderr"].read()))
                raise
        with patch.object(subprocess, "run", side_effect=slow_worker):
            with self.assertRaisesRegex(OSError, "timed out"):
                self.store.snapshot(A, V, None, "outline")
        self.assertEqual(len(pids), 1)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pids[0], os.WNOHANG)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)
        self.assertNotIn("snapshot_file", self.store.views[V])


if __name__ == "__main__":
    unittest.main()
